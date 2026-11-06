import argparse
from pathlib import Path
import re

parser = argparse.ArgumentParser(description="Render one independent Nginx subscription site")
parser.add_argument("--domain", required=True)
for field in ("data-dir", "runtime-dir", "cert-file", "key-file", "auth-file"):
    parser.add_argument("--" + field, required=True)
parser.add_argument("--legacy-owner", default="", help="Also serve legacy /token/raw/file and /owner/token/raw/file links")
args = parser.parse_args()
if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", args.domain):
    parser.error("Domain must be a lowercase hostname")
values = vars(args).copy()
owner = values.pop("legacy_owner")
if owner and not re.fullmatch(r"[A-Za-z0-9-]{1,39}", owner):
    parser.error("Legacy owner must be a GitHub username")
for key, value in values.items():
    if key != "domain" and not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
        parser.error("Use absolute paths containing letters, digits, slash, dot, hyphen or underscore")
template = Path(__file__).with_name("nginx.conf.template").read_text()
template = template.replace("@@LEGACY_OWNER@@", owner)
for key, value in values.items():
    template = template.replace("@@" + key.upper() + "@@", value)
if "@@" in template:
    raise RuntimeError("Unresolved template field")
print(template, end="")
