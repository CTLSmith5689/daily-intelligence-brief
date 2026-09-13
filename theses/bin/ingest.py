#!/usr/bin/env python3
"""Move finished analyst runs from Google Drive into the archive.

The analyst runs as a cloud routine. It can clone this public repository but not
push to it: the cloud git proxy only injects a credential for repositories in a
session's source list, and a scheduled task has no way to set one. It can write
to Google Drive through the owner's connector, as the owner. So each run is
delivered to a folder there, and this script, run by the hourly workflow, brings
it in.

For a cloud run this is the only thing that writes to the ledger. The analyst
validates its own notes, but a check the writer runs on itself is not a gate, so
everything is checked again here against the committed code, and a run that
fails any check is refused whole and left in Drive:

- A run is picked up only once its folder holds ingest.json, which the analyst
  uploads last. A half-uploaded run is invisible.
- A Drive file name encodes a repository path, "/" written as "__". It must
  decode to one of two shapes, a note or the run manifest, and be listed in
  ingest.json. Files without the "theses__" prefix, such as dossiers kept for a
  reader, are ignored.
- Each file's sha256 must match the hash the analyst computed in its sandbox.
  The connector uploads text pasted into a tool call, so without this a note
  could reach the archive that differs from the one that passed validation.
- Notes are never edited. A path that already exists with different bytes
  refuses the run.
- validate.py and events.py run here. If either fails, every file written and
  every ledger row appended for the run is rolled back.

A refused run is recorded with a fingerprint of its folder and is not looked at
again until its files change, so one bad delivery raises one alert, not one an
hour.

    GDRIVE_SA_KEY='{...}' python3 theses/bin/ingest.py
    GDRIVE_SA_KEY='{...}' python3 theses/bin/ingest.py --dry-run
"""
import base64, hashlib, json, os, re, shlex, subprocess, sys, tempfile, time, traceback
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
from common import REPO, THESES, LEDGER, read_csv_rows, append_csv
import validate

# The "Investment Research" folder in the owner's Drive. Not a secret: an id is
# useless without access, and the service account reads only what is shared
# with it.
FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID") or "1J6Wk6dzAg92lgrbAnLqVbQLXXKy1qck6"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
TOKEN_URI = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"

MARKER = "ingest.json"
RUN_FOLDER = re.compile(r"^run-(\d{4}-\d{2}-\d{2})$")
DRIVE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TICKER = r"[A-Z][A-Z0-9.\-]{0,9}"
_DATE = r"\d{4}-\d{2}-\d{2}"
NOTE_PATH = re.compile(rf"^theses/notes/({_TICKER})/({_DATE})-(initiation|update|revision|close)\.md$")
MANIFEST_PATH = re.compile(rf"^theses/runs/({_DATE})/manifest\.json$")
MAX_BYTES = {"note": 200_000, "manifest": 500_000, "marker": 100_000}
FIELD_MAX = 300
PENDING_WARN_HOURS = 48

INGESTED = LEDGER / "ingested.csv"
INGESTED_COLUMNS = ["drive_folder_id", "folder_name", "run_date", "notes", "ingested_at"]
REFUSED = LEDGER / "ingest_refused.csv"
REFUSED_COLUMNS = ["drive_folder_id", "folder_name", "fingerprint", "reason", "refused_at"]


class Refuse(Exception):
    """The delivery broke the contract. Refused whole and left in Drive."""


class DriveError(Exception):
    """Drive or the token exchange failed. Nothing about the run is decided."""


def now_iso():
    return datetime.now(tz=timezone.utc).isoformat(timespec="seconds")


def tail(text, n=15):
    return "\n".join((text or "").strip().splitlines()[-n:])


def clean_field(value):
    """A ledger cell from free text: one line, no control characters, capped."""
    s = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value or ""))
    return re.sub(r"\s+", " ", s).strip()[:FIELD_MAX]


def decode_name(name):
    """The repository path a Drive file name encodes, or None if it encodes none."""
    return name.replace("__", "/") if name.startswith("theses__") else None


def kind_of(path):
    if NOTE_PATH.match(path):
        return "note"
    if MANIFEST_PATH.match(path):
        return "manifest"
    return None


def fingerprint(files):
    rows = sorted([f.get("id", ""), f.get("name", ""), f.get("modifiedTime", ""),
                   str(f.get("size", ""))] for f in files)
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]


def _age_hours(item):
    try:
        t = datetime.strptime(str(item.get("createdTime", ""))[:19], "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return 0.0
    return (datetime.now(tz=timezone.utc) - t.replace(tzinfo=timezone.utc)).total_seconds() / 3600


class Drive:
    """Read-only access to the delivery folder through a service account.

    Stdlib only. Python has no RSA, so the token request is signed by the openssl
    binary every GitHub runner has. The alternative was the Google client
    libraries: 21 packages and 131 MB to make three kinds of HTTP call."""

    def __init__(self, key_json):
        try:
            sa = json.loads(key_json)
            self._email, self._pem = sa["client_email"], sa["private_key"]
        except (ValueError, KeyError, TypeError):
            # Never echo the value. It is a private key.
            raise DriveError("GDRIVE_SA_KEY is not a service account JSON key") from None
        self._token, self._expires = None, 0.0

    def _assertion(self):
        b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=")
        now = int(time.time())
        head = b64u(b'{"alg":"RS256","typ":"JWT"}')
        claim = b64u(json.dumps({"iss": self._email, "scope": SCOPE, "aud": TOKEN_URI,
                                 "iat": now, "exp": now + 3600}, separators=(",", ":")).encode())
        signing = head + b"." + claim
        fd, keyfile = tempfile.mkstemp(suffix=".pem")      # mkstemp creates it 0600
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(self._pem)
            proc = subprocess.run(["openssl", "dgst", "-sha256", "-sign", keyfile],
                                  input=signing, capture_output=True)
        finally:
            os.unlink(keyfile)
        if proc.returncode != 0:
            raise DriveError("openssl could not sign the token request with GDRIVE_SA_KEY")
        return signing + b"." + b64u(proc.stdout)

    def _access_token(self):
        if self._token and time.time() < self._expires - 300:
            return self._token
        body = urllib.parse.urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                       "assertion": self._assertion().decode()}).encode()
        try:
            with urllib.request.urlopen(TOKEN_URI, body, timeout=30) as resp:
                self._token = json.load(resp)["access_token"]
        except urllib.error.HTTPError as exc:
            raise DriveError(f"the token exchange was refused: HTTP {exc.code} {exc.read()[:200]!r}") from None
        except (urllib.error.URLError, TimeoutError, KeyError, ValueError) as exc:
            raise DriveError(f"the token exchange failed: {type(exc).__name__}") from None
        self._expires = time.time() + 3600
        return self._token

    def _get(self, url, raw=False):
        problem = ""
        for attempt in range(4):
            if attempt:
                time.sleep(2 ** attempt)
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._access_token()}"})
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = resp.read()
                return body if raw else json.loads(body)
            except urllib.error.HTTPError as exc:
                problem = f"HTTP {exc.code} {exc.read()[:200]!r}"
                if exc.code not in (429, 500, 502, 503, 504):
                    break
            except (urllib.error.URLError, TimeoutError) as exc:
                problem = type(exc).__name__
            except ValueError:
                problem = "the response was not JSON"
                break
        raise DriveError(f"{url.split('?')[0]}: {problem}")

    def children(self, folder_id):
        if not DRIVE_ID.match(folder_id or ""):
            raise DriveError(f"not a Drive id: {folder_id!r}")
        params = {"q": f"'{folder_id}' in parents and trashed = false",
                  "fields": "nextPageToken,files(id,name,mimeType,size,modifiedTime,createdTime)",
                  "pageSize": "1000", "orderBy": "name",
                  "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
        out = []
        while True:
            page = self._get(f"{API}?{urllib.parse.urlencode(params)}")
            out.extend(page.get("files") or [])
            if not page.get("nextPageToken"):
                return out
            params["pageToken"] = page["nextPageToken"]

    def download(self, file_id):
        if not DRIVE_ID.match(file_id or ""):
            raise DriveError(f"not a Drive id: {file_id!r}")
        return self._get(f"{API}/{file_id}?alt=media&supportsAllDrives=true", raw=True)


class Txn:
    """Every path a run writes, with its bytes beforehand, so a failure restores them."""

    def __init__(self):
        self.saved = {}

    def touch(self, path):
        path = Path(path)
        if path not in self.saved:
            self.saved[path] = path.read_bytes() if path.exists() else None

    def write(self, path, data):
        self.touch(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def rollback(self):
        for path, data in self.saved.items():
            if data is None:
                if path.exists():
                    path.unlink()
            else:
                path.write_bytes(data)
        # Directories this run created and then emptied, deepest first.
        for d in sorted({p.parent for p in self.saved}, key=lambda p: len(p.parts), reverse=True):
            while THESES in d.parents and d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                d = d.parent


def _load_json(drive, f, limit, label):
    if str(f.get("mimeType", "")).startswith("application/vnd.google-apps."):
        raise Refuse(f"{label} was uploaded as a Google-native file, not plain JSON")
    data = drive.download(f["id"])
    if len(data) > limit:
        raise Refuse(f"{label} is {len(data):,} bytes, over the {limit:,} limit")
    try:
        spec = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise Refuse(f"{label} is not valid JSON ({exc})")
    if not isinstance(spec, dict):
        raise Refuse(f"{label} is not a JSON object")
    return spec


def _check_content(path, kind, data, run_date):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refuse(f"{path} is not UTF-8 text")
    if kind == "manifest":
        try:
            man = json.loads(text)
        except ValueError as exc:
            raise Refuse(f"{path} is not valid JSON ({exc})")
        if not isinstance(man, dict) or man.get("run_date") != run_date:
            raise Refuse(f"{path} does not carry run_date {run_date}")
        return
    fm, _body = validate.parse(text)
    if not fm:
        raise Refuse(f"{path} has no front-matter")
    g = lambda k: str(fm.get(k) or "").strip().strip('"').strip("'")
    ticker = NOTE_PATH.match(path).group(1)
    if g("ticker") != ticker:
        raise Refuse(f"{path} is filed under {ticker} but its front-matter says {g('ticker')!r}")
    if g("written_on") != run_date:
        raise Refuse(f"{path} says written_on {g('written_on')!r}, not the run date {run_date}")


def process_run(drive, folder, files, recorded, dry_run=False):
    """Check one delivered run and, unless dry_run, bring it into the archive."""
    run_date = RUN_FOLDER.match(folder["name"]).group(1)
    by_name = {}
    for f in files:
        by_name.setdefault(f.get("name", ""), []).append(f)

    markers = by_name.get(MARKER) or []
    if not markers:
        return {"status": "pending", "run_date": run_date, "tickers": []}
    if len(markers) > 1:
        raise Refuse("the folder holds more than one ingest.json")
    spec = _load_json(drive, markers[0], MAX_BYTES["marker"], MARKER)

    if spec.get("run_date") != run_date:
        raise Refuse(f"ingest.json says run_date {spec.get('run_date')!r} but the folder is {folder['name']}")
    notes, hashes = spec.get("notes"), spec.get("sha256")
    if not isinstance(notes, list) or not notes:
        raise Refuse("ingest.json lists no notes")
    if not isinstance(hashes, dict):
        raise Refuse("ingest.json has no sha256 map")

    expected = {f"theses/runs/{run_date}/manifest.json": "manifest"}
    entries = []
    for n in notes:
        if not isinstance(n, dict) or not isinstance(n.get("path"), str):
            raise Refuse("every entry in notes needs a path")
        m = NOTE_PATH.match(n["path"])
        if not m:
            raise Refuse(f"{n['path']!r} is not a note path")
        if m.group(2) != run_date:
            raise Refuse(f"{n['path']} is dated {m.group(2)}, not the run date {run_date}")
        if n["path"] in expected:
            raise Refuse(f"{n['path']} is listed twice")
        expected[n["path"]] = "note"
        entries.append({"path": n["path"], "ticker": m.group(1),
                        "trigger": clean_field(n.get("trigger")),
                        "rationale": clean_field(n.get("rationale"))})

    delivered = {}
    for name, same in by_name.items():
        path = decode_name(name)
        if path is None:
            continue                          # dossiers and anything else meant for a reader
        if kind_of(path) is None:
            raise Refuse(f"{name!r} decodes to {path!r}, which is outside the allowlist")
        if path not in expected:
            raise Refuse(f"{name} is in the folder but not listed in ingest.json")
        if len(same) > 1:
            raise Refuse(f"{name} appears {len(same)} times in the folder")
        f = same[0]
        if str(f.get("mimeType", "")).startswith("application/vnd.google-apps."):
            raise Refuse(f"{name} was uploaded as {f['mimeType']}, not a plain file; "
                         "it needs disableConversionToGoogleType")
        delivered[path] = f
    for path in expected:
        if path not in delivered:
            raise Refuse(f"ingest.json lists {path} but the folder has no {path.replace('/', '__')}")
        if not (isinstance(hashes.get(path), str) and SHA256.match(hashes[path])):
            raise Refuse(f"ingest.json has no valid sha256 for {path}")

    payload, present = {}, {}
    for path, f in delivered.items():
        kind = expected[path]
        data = drive.download(f["id"])
        if len(data) > MAX_BYTES[kind]:
            raise Refuse(f"{path} is {len(data):,} bytes, over the {MAX_BYTES[kind]:,} limit for a {kind}")
        if hashlib.sha256(data).hexdigest() != hashes[path]:
            raise Refuse(f"{path} does not match its sha256 in ingest.json, so the uploaded bytes "
                         "are not the file the analyst validated")
        _check_content(path, kind, data, run_date)
        target = REPO / path
        if target.exists() and target.read_bytes() != data:
            raise Refuse(f"{path} already exists with different content; notes are never edited")
        present[path] = target.exists()
        payload[path] = data

    tickers = [e["ticker"] for e in entries]
    todo = [e for e in entries if e["path"] not in recorded]
    if all(present.values()) and not todo:
        if not dry_run:
            append_csv(INGESTED, INGESTED_COLUMNS, [{
                "drive_folder_id": folder["id"], "folder_name": folder["name"], "run_date": run_date,
                "notes": " ".join(tickers), "ingested_at": now_iso()}])
        return {"status": "already", "run_date": run_date, "tickers": tickers}
    if dry_run:
        return {"status": "would_ingest", "run_date": run_date, "tickers": tickers}

    txn = Txn()
    try:
        for path, data in payload.items():
            if not present[path]:
                txn.write(REPO / path, data)
        check = subprocess.run([sys.executable, str(BIN / "validate.py"), *[e["path"] for e in entries]],
                               cwd=REPO, capture_output=True, text=True)
        if check.returncode != 0:
            raise Refuse("validate.py failed:\n" + tail(check.stdout + check.stderr))
        for e in todo:
            for p in (LEDGER / "events.csv", LEDGER / "predictions.csv",
                      THESES / "positions" / f"{e['ticker']}.md"):
                txn.touch(p)
            rec = subprocess.run([sys.executable, str(BIN / "events.py"), e["path"], e["trigger"], e["rationale"]],
                                 cwd=REPO, capture_output=True, text=True)
            if rec.returncode != 0:
                # Not the analyst's fault, so not a refusal: a refusal is not retried
                # until the delivery changes, and this needs a fix in the repository.
                raise RuntimeError(f"events.py failed on {e['path']}:\n" + tail(rec.stdout + rec.stderr))
        txn.touch(INGESTED)
        append_csv(INGESTED, INGESTED_COLUMNS, [{
            "drive_folder_id": folder["id"], "folder_name": folder["name"], "run_date": run_date,
            "notes": " ".join(tickers), "ingested_at": now_iso()}])
    except BaseException:
        txn.rollback()
        raise
    return {"status": "ingested", "run_date": run_date, "tickers": tickers}


def finish(result, dry_run):
    ing = [r for r in result["ingested"] if r["status"] == "ingested"]
    tickers = [t for r in ing for t in r["tickers"]]
    if ing:
        dates = sorted({r["run_date"] for r in ing})
        msg = f"theses({', '.join(dates)}): {', '.join(tickers)} [delivered via Drive]"
    elif result["refused_new"]:
        msg = "ingest: refused " + ", ".join(name for name, _ in result["refused_new"])
    else:
        msg = "ingest: record runs already in the archive"
    for e in result["errors"]:
        print(f"ingest: ERROR {e}")
    env = {"INGESTED_RUNS": len(ing), "INGESTED_NOTES": len(tickers),
           "REFUSED_NEW": len(result["refused_new"]), "INGEST_ERRORS": len(result["errors"]),
           "INGEST_ALERT": int(bool(result["refused_new"] or result["errors"])),
           "INGEST_MESSAGE": msg}
    out = os.environ.get("INGEST_RESULT")
    if out and not dry_run:
        with open(out, "w", encoding="utf-8") as fh:
            for k, v in env.items():
                fh.write(f"{k}={shlex.quote(str(v))}\n")
    print("ingest: " + json.dumps(env))
    return 0


def main(argv=None, drive=None):
    argv = sys.argv[1:] if argv is None else argv
    dry_run = "--dry-run" in argv
    result = {"ingested": [], "refused_new": [], "errors": []}

    if drive is None:
        key = os.environ.get("GDRIVE_SA_KEY", "").strip()
        if not key:
            print("ingest: GDRIVE_SA_KEY is not set, so there is no Drive to read. Nothing to do.")
            return finish(result, dry_run)
        try:
            drive = Drive(key)
        except DriveError as exc:
            result["errors"].append(str(exc))
            return finish(result, dry_run)

    done = {r.get("drive_folder_id") for r in read_csv_rows(INGESTED)}
    refused = {(r.get("drive_folder_id"), r.get("fingerprint")) for r in read_csv_rows(REFUSED)}
    try:
        top = drive.children(FOLDER_ID)
    except DriveError as exc:
        result["errors"].append(f"listing the delivery folder: {exc}")
        return finish(result, dry_run)

    runs = sorted((f for f in top if f.get("mimeType") == FOLDER_MIME
                   and RUN_FOLDER.match(f.get("name", ""))), key=lambda f: f["name"])
    print(f"ingest: {len(runs)} run folder(s) in Drive, "
          f"{sum(1 for f in runs if f['id'] in done)} already ingested.")
    for folder in runs:
        if folder["id"] in done:
            continue
        name, fp = folder["name"], ""
        try:
            files = drive.children(folder["id"])
            fp = fingerprint(files)
            if (folder["id"], fp) in refused:
                print(f"ingest: {name} was refused before and has not changed since; skipping.")
                continue
            recorded = {r.get("note_path") for r in read_csv_rows(LEDGER / "events.csv")}
            out = process_run(drive, folder, files, recorded, dry_run)
        except Refuse as exc:
            reason = str(exc)
            print(f"ingest: REFUSED {name}: {reason}")
            if not dry_run:
                append_csv(REFUSED, REFUSED_COLUMNS, [{
                    "drive_folder_id": folder["id"], "folder_name": name, "fingerprint": fp,
                    "reason": clean_field(reason.splitlines()[0]), "refused_at": now_iso()}])
            result["refused_new"].append((name, reason))
            continue
        except DriveError as exc:
            result["errors"].append(f"{name}: {exc}")
            break
        except (Exception, SystemExit) as exc:
            print(traceback.format_exc())
            result["errors"].append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        if out["status"] == "pending":
            age = _age_hours(folder)
            note = f", and it is {age:.0f} hours old" if age > PENDING_WARN_HOURS else ""
            print(f"ingest: {name} has no ingest.json yet, so it is still being delivered{note}.")
        else:
            print(f"ingest: {out['status']} {name}: {', '.join(out['tickers'])}")
            if out["status"] != "would_ingest":
                result["ingested"].append(out)
    return finish(result, dry_run)


if __name__ == "__main__":
    sys.exit(main())
