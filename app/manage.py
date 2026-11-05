import argparse
import csv
import os
from pathlib import Path
import secrets

from store import BASE, canonical_token, canonical_uuid, folder_for, publish, validate_template

parser = argparse.ArgumentParser(description="Initialize an independent subscription instance")
parser.add_argument("--users", type=Path, required=True, help="Subscriber CSV or legacy Gist manifest")
parser.add_argument("--template", type=Path, required=True, help="YAML template containing {{UUID}}")
args = parser.parse_args()
os.umask(0o077)
if (BASE / "current").exists():
    parser.error("This instance is initialized")
template = args.template.read_bytes().decode("utf-8")
validate_template(template)
users = []
tokens = set()
with args.users.open(encoding="utf-8-sig", newline="") as file:
    reader = csv.DictReader(file)
    legacy = reader.fieldnames == ["Gist ID", "UUID", "备注", "哈希值"]
    if not legacy and reader.fieldnames not in (["email", "uuid"], ["email", "uuid", "token"]):
        parser.error("Use email,uuid[,token] or a legacy Gist manifest")
    for number, row in enumerate(reader, 2):
        if None in row or any(value is None for value in row.values()):
            parser.error(f"Invalid CSV column count on row {number}")
        if legacy:
            filename = row["备注"].strip()
            if not filename.endswith(".yaml"):
                parser.error(f"Expected a .yaml filename on row {number}")
            email = filename[:-5]
            credential = canonical_uuid(row["UUID"].strip())
            token = canonical_token(row["Gist ID"])
            folder = folder_for(email)
            if folder != filename:
                parser.error(f"Filename changes during normalization on row {number}")
        else:
            email = row["email"].strip()
            folder = folder_for(email)
            credential = canonical_uuid(row["uuid"].strip())
            supplied = row.get("token", "").strip()
            token = canonical_token(supplied) if supplied else secrets.token_hex(32)
        if token in tokens:
            parser.error(f"Duplicate token on row {number}")
        tokens.add(token)
        users.append(dict(id=token, email=email, folder=folder, uuid=credential, token=token, active=True))
BASE.mkdir(parents=True, exist_ok=True)
publish(users, template)
print(f"Initialized {len(users)} subscriptions, preserving supplied tokens and filenames")
