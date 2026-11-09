import sys, json, urllib.request, urllib.error

BASE = "https://www.smartsolity.com"
email = sys.argv[1]
hashed = sys.argv[2]


def call(path, body, headers):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, headers=headers,
        method=("POST" if data else "GET"),
    )
    r = urllib.request.urlopen(req, timeout=30)
    return r.status, r.read().decode("utf-8", "replace")


lb = {
    "emailId": email,
    "hashedPwd": hashed,
    "phoneToken": "ha-solity",
    "appSource": "0",
    "lang": "0",
}
st, txt = call("/api_v2/login", lb, {"Content-Type": "application/json"})
d = json.loads(txt)
c = d.get("contents") or {}
tok = c.get("token")
tokpwd = c.get("tokenPwd")
print("LOGIN", st, "loginResult", c.get("loginResult"), "hasToken", bool(tok))
if not tok:
    print("LOGIN_FAIL", txt[:300])
    sys.exit(1)

h = {"Content-Type": "application/json", "Authorization": tok}
if tokpwd:
    h["AuthorizationPwd"] = tokpwd
st2, txt2 = call("/api_v2/myDevice", None, h)
dd = json.loads(txt2)
cc = dd.get("contents") or {}
print("MYDEVICE", st2)
for key, val in cc.items():
    if isinstance(val, list):
        for it in val:
            if isinstance(it, dict) and it.get("myDeviceId"):
                print("DEV", json.dumps({
                    "list": key,
                    "id": it.get("myDeviceId"),
                    "nick": it.get("myDeviceNickName"),
                    "bleMac": it.get("myDeviceBleMacAddr"),
                    "regDeviceAppKey": it.get("regDeviceAppKey"),
                    "regWifiAppKey": it.get("regWifiAppKey"),
                    "memberIdx": it.get("memberIdx"),
                    "memberId": it.get("myDeviceMemberId"),
                }, ensure_ascii=False))

print("CONTENTS_KEYS", list(cc.keys()))
print("RAW", json.dumps(cc, ensure_ascii=False)[:2500])
