import argparse
import csv
import os
from pathlib import Path
import secrets

from store import BASE, canonical_uuid, folder_for, publish, validate_template

parser = argparse.ArgumentParser(description="Initialize an independent subscription instance")
parser.add_argument("--users", type=Path, required=True, help="CSV with email,uuid headers")
parser.add_argument("--template", type=Path, required=True, help="YAML template containing {{UUID}}")
args = parser.parse_args()
os.umask(0o077)
if (BASE / "current").exists():
    parser.error("This instance is initialized")
template = args.template.read_bytes().decode("utf-8")
validate_template(template)
users = []
names, credentials = set(), set()
with args.users.open(encoding="utf-8-sig", newline="") as file:
    reader = csv.DictReader(file)
    if reader.fieldnames != ["email", "uuid"]:
        parser.error("CSV headers must be email,uuid")
    for row in reader:
        email = row["email"].strip()
        folder = folder_for(email)
        credential = canonical_uuid(row["uuid"].strip())
        if folder.casefold() in names or credential in credentials:
            parser.error("Duplicate subscriber or UUID")
        names.add(folder.casefold())
        credentials.add(credential)
        users.append(dict(email=email, folder=folder, uuid=credential,
                          token=secrets.token_hex(32), active=True))
BASE.mkdir(parents=True, exist_ok=True)
publish(users, template)
print(f"Initialized {len(users)} subscribers")
