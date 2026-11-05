import json
import os
from pathlib import Path
import re
import secrets
import shutil
from urllib.parse import quote
import uuid
import yaml

BASE = Path(os.environ.get("DATA_DIR", "/data"))
ORIGIN = os.environ["PUBLIC_ORIGIN"].rstrip("/")
WEB_GID = int(os.environ.get("WEB_GID", "33"))
FOLDER = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9_.+-]{0,199}[.]yaml")
GENERATION = re.compile(r"gen-[a-f0-9]{32}")
MARKER = "{{UUID}}"
MAX_TEMPLATE_BYTES = 512 * 1024


class InputError(ValueError):
    pass


def folder_for(email):
    folder = email.rsplit("@", 1)[0] + ".yaml"
    if not FOLDER.fullmatch(folder) or not email.isascii() or len(email) > 254 or any(c.isspace() for c in email):
        raise InputError("邮箱或目录名格式不正确")
    return folder


def canonical_uuid(value):
    try:
        parsed = str(uuid.UUID(value))
    except ValueError:
        raise InputError("UUID 格式不正确") from None
    if parsed != value.lower():
        raise InputError("请输入带连字符的 UUID")
    return parsed


def canonical_token(value):
    token = value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._~-]{32,255}", token):
        raise InputError("令牌支持 32～255 个英文字母、数字或 . _ ~ -")
    if token in {".", "..", "panel"}:
        raise InputError("该令牌是系统保留路径，请换一个")
    return token


def user_id(user):
    return user.get("id", user.get("usage_key", user["token"]))


def find_user(users, identity="", folder=""):
    if identity:
        return next((user for user in users if user_id(user) == identity), None)
    matches = [user for user in users if user["folder"] == folder]
    return matches[0] if len(matches) == 1 else None


def subscription(user):
    return f"{ORIGIN}/{user['token']}/{quote(user['folder'], safe='+')}"


def validate_template(template):
    if len(template.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise InputError("模板超过 512 KiB")
    if MARKER not in template:
        raise InputError("模板必须包含 {{UUID}}")
    try:
        config = yaml.safe_load(template.replace(MARKER, "00000000-0000-4000-8000-000000000000"))
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line = f"（第 {mark.line + 1} 行）" if mark else ""
        raise InputError(f"YAML 语法无效{line}") from None
    if not isinstance(config, dict) or not (config.get("proxies") or config.get("proxy-providers")):
        raise InputError("模板需要包含 proxies 或 proxy-providers")


def read():
    version = os.readlink(BASE / "current")
    if not GENERATION.fullmatch(version):
        raise RuntimeError("Invalid subscription generation")
    directory = BASE / version
    users = json.loads((directory / "users.json").read_bytes())
    template = (directory / "template.yaml").read_bytes().decode("utf-8")
    return version, users, template


def publish(users, template):
    """Build a complete private snapshot, then switch the HTTPS document root."""
    group = WEB_GID
    version = "gen-" + secrets.token_hex(16)
    stage = BASE / version
    pending = BASE / (".next-" + version)
    current = BASE / "current"
    previous = os.readlink(current) if current.is_symlink() else None

    def directory(path):
        path.mkdir(exist_ok=True)
        os.chown(path, -1, group)
        path.chmod(0o750)

    directory(stage)
    try:
        (stage / "users.json").write_text(json.dumps(users, ensure_ascii=False), encoding="utf-8")
        (stage / "template.yaml").write_bytes(template.encode("utf-8"))
        directory(stage / "live")
        (stage / "revoked").mkdir()
        for user in users:
            status = "live" if user["active"] else "revoked"
            account = stage / status / user["folder"]
            directory(account)
            directory(account / user["token"])
            profile = account / user["token"] / "clash.yaml"
            profile.write_bytes(template.replace(MARKER, user["uuid"]).encode("utf-8"))
            os.chown(profile, -1, group)
            profile.chmod(0o640)
        pending.symlink_to(version, target_is_directory=True)
        pending.replace(current)
    except BaseException:
        pending.unlink(missing_ok=True)
        if not current.is_symlink() or os.readlink(current) != version:
            shutil.rmtree(stage)
        raise
    for item in BASE.iterdir():
        if GENERATION.fullmatch(item.name) and item.name not in {version, previous} and item.is_dir() and not item.is_symlink():
            shutil.rmtree(item, ignore_errors=True)
