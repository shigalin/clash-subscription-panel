import json
from datetime import datetime
import ipaddress
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
from urllib.parse import quote, urlsplit
import uuid
import yaml
from zoneinfo import ZoneInfo

BASE = Path(os.environ.get("DATA_DIR", "/data"))
ORIGIN = os.environ["PUBLIC_ORIGIN"].rstrip("/")
WEB_GID = int(os.environ.get("WEB_GID", "33"))
FOLDER = re.compile(r"[A-Za-z0-9_+-][A-Za-z0-9_.+-]{0,199}[.]yaml")
GENERATION = re.compile(r"gen-[a-f0-9]{32}")
MARKER = "{{UUID}}"
MAX_TEMPLATE_BYTES = 512 * 1024
MAX_UA_RULES = 50
MAX_UA_PATTERN = 256
TIMEZONE = ZoneInfo("Asia/Taipei")
ENHANCED_UA = re.compile(
    r"^(?P<product>[a-z][a-z0-9.-]*)/v?"
    r"(?P<version>[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5})"
    r"(?P<suffix>\.Meta)?(?=\s|$)",
    re.IGNORECASE,
)
ENHANCED_MINIMUM = {
    "clashmetaforandroid": (2, 11, 37),
    "clash.meta": (1, 19, 34),
    "clash-verge": (2, 5, 11),
    "mihomo.party": (2, 0, 5),
}


def supports_enhanced(agent):
    match = ENHANCED_UA.match(agent.strip())
    if not match:
        return False
    product = match["product"].lower()
    minimum = ENHANCED_MINIMUM.get(product)
    if minimum is None or (match["suffix"] and product != "clashmetaforandroid"):
        return False
    version = tuple(map(int, match["version"].split(".")))
    return version >= minimum


class InputError(ValueError):
    pass


def validate_ua_pattern(pattern):
    if not pattern.strip() or len(pattern) > MAX_UA_PATTERN or not pattern.isprintable():
        raise InputError(f"正则需包含 1～{MAX_UA_PATTERN} 个可见字符")
    try:
        re.compile(pattern)
    except (re.error, OverflowError, RecursionError):
        raise InputError("正则表达式无效，请检查语法") from None


def _ua_timeout(_signum, _frame):
    raise TimeoutError("UA regex timed out")


def match_ua_rule(rules, agent):
    # Requests run on the main thread. Bound the whole match pass because
    # Python's regular expressions can backtrack on client-controlled input.
    if not rules:
        return None
    previous = signal.signal(signal.SIGALRM, _ua_timeout)
    try:
        signal.setitimer(signal.ITIMER_REAL, 0.025)
        return next((rule for rule in rules if rule["active"]
                     and re.search(rule["pattern"], agent)), None)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


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


def canonical_expiry(value):
    if not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=TIMEZONE)
        return parsed.astimezone(TIMEZONE).isoformat(timespec="seconds")
    except ValueError:
        raise InputError("到期时间格式不正确") from None


def expiry_time(item):
    value = item.get("expires_at")
    return datetime.fromisoformat(value) if value else None


def item_state(item, now=None):
    if not item["active"]:
        return "revoked"
    expiry = expiry_time(item)
    return "expired" if expiry and expiry <= (now or datetime.now(TIMEZONE)) else "live"


def display_expiry(item, form=False):
    expiry = expiry_time(item)
    if not expiry:
        return "" if form else "长期有效"
    return expiry.astimezone(TIMEZONE).strftime("%Y-%m-%dT%H:%M" if form else "%Y-%m-%d %H:%M:%S")


def canonical_forward(path, target):
    path, target = path.strip(), target.strip()
    parts = path.split("/")
    if (not re.fullmatch(r"/[A-Za-z0-9._~+/-]{1,2047}", path)
            or any(part in {"", ".", ".."} for part in parts[1:])
            or parts[1] in {"panel", "internal", "health"} or parts[1].startswith("_")):
        raise InputError("请求路径需以 / 开头，不含查询参数、空格、空目录或系统保留路径")
    try:
        parsed = urlsplit(target)
        host = parsed.hostname or ""
        if (len(target) > 4096 or not re.fullmatch(r"[\x21-\x7e]+", target)
                or "\\" in target or parsed.scheme != "https" or parsed.username is not None
                or parsed.password is not None or parsed.port not in {None, 443} or parsed.fragment
                or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host)
                or "." not in host or host.endswith((".localhost", ".local"))):
            raise ValueError
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            raise ValueError
    except ValueError:
        raise InputError("目标需为 HTTPS 域名地址，端口限 443，不支持登录凭据、IP 地址或片段") from None
    return path, target, host


def find_subscription(users, path, legacy_owner=""):
    prefix = rf"(?:{re.escape(legacy_owner)}/)?" if legacy_owner else ""
    patterns = [rf"/([A-Za-z0-9._~-]{{32,255}})/({FOLDER.pattern})"]
    if legacy_owner:
        patterns.append(rf"/{prefix}([A-Za-z0-9._~-]{{32,255}})/raw/({FOLDER.pattern})")
    for pattern in patterns:
        match = re.fullmatch(pattern, path)
        if match:
            token, folder = match.groups()
            return next((u for u in users if u["token"] == token and u["folder"] == folder), None)
    return None


def validate_template(template, *, require_marker=True):
    if len(template.encode("utf-8")) > MAX_TEMPLATE_BYTES:
        raise InputError("模板超过 512 KiB")
    if require_marker and MARKER not in template:
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


def read_forwards(version=None):
    if version is None:
        if not (BASE / "current").is_symlink():
            return []
        version = os.readlink(BASE / "current")
    if not GENERATION.fullmatch(version):
        raise RuntimeError("Invalid subscription generation")
    source = BASE / version / "forwards.json"
    return json.loads(source.read_bytes()) if source.exists() else []


def read_enhanced(version):
    if version is None:
        return ""
    if not GENERATION.fullmatch(version):
        raise RuntimeError("Invalid subscription generation")
    try:
        return (BASE / version / "enhanced.yaml").read_bytes().decode("utf-8")
    except FileNotFoundError:
        return ""


def read_ua_rules(version):
    if version is None:
        return []
    if not GENERATION.fullmatch(version):
        raise RuntimeError("Invalid subscription generation")
    try:
        return json.loads((BASE / version / "ua-rules.json").read_bytes())
    except FileNotFoundError:
        return []


def publish(users, template, forwards=None, *, enhanced=None, ua_rules=None):
    """Build a complete private snapshot, then switch the HTTPS document root."""
    group = WEB_GID
    version = "gen-" + secrets.token_hex(16)
    stage = BASE / version
    pending = BASE / (".next-" + version)
    current = BASE / "current"
    previous = os.readlink(current) if current.is_symlink() else None
    if forwards is None:
        forwards = read_forwards(previous) if previous else []
    if enhanced is None:
        enhanced = read_enhanced(previous)
    if ua_rules is None:
        ua_rules = read_ua_rules(previous)

    def directory(path):
        path.mkdir(exist_ok=True)
        os.chown(path, -1, group)
        path.chmod(0o750)

    directory(stage)
    try:
        (stage / "users.json").write_text(json.dumps(users, ensure_ascii=False), encoding="utf-8")
        (stage / "forwards.json").write_text(json.dumps(forwards, ensure_ascii=False), encoding="utf-8")
        (stage / "template.yaml").write_bytes(template.encode("utf-8"))
        if enhanced:
            source = stage / "enhanced.yaml"
            source.write_bytes(enhanced.encode("utf-8"))
            source.chmod(0o600)
        if ua_rules:
            source = stage / "ua-rules.json"
            source.write_text(json.dumps(ua_rules, ensure_ascii=False), encoding="utf-8")
            source.chmod(0o600)
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
