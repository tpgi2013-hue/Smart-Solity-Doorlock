import sys, json, hashlib, base64, urllib.request, urllib.error
BASE = "https://www.smartsolity.com"
email = sys.argv[1]; pw = sys.argv[2]
hashed = base64.b64encode(hashlib.sha256(pw.encode()).digest()).decode()
def call(method, path, body, headers):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE+path, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=30); return r.status, r.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8","replace")
st, txt = call("POST","/api_v2/login", {"emailId":email,"hashedPwd":hashed,"phoneToken":"ha-solity","appSource":"0","lang":"0"}, {"Content-Type":"application/json"})
d = json.loads(txt); c = d.get("contents") or {}
mi = c.get("memberInfo") or {}
print("LOGIN", st, "result", c.get("loginResult"), "member", mi.get("memberName"), "type", mi.get("MEMBER_TYPE"))
tok = c.get("token"); tokpwd = c.get("tokenPwd")
if not tok:
    print("NO_TOKEN", txt[:200]); sys.exit(0)
h = {"Content-Type":"application/json","Authorization":tok}
if tokpwd: h["AuthorizationPwd"] = tokpwd
st2, txt2 = call("GET","/api_v2/myDevice", None, h)
cc = (json.loads(txt2).get("contents") or {})
print("MYDEVICE", st2, "count", cc.get("myDeviceListCount"))
for key, val in cc.items():
    if isinstance(val, list):
        for it in val:
            if isinstance(it, dict) and it.get("myDeviceId"):
                print("DEV", json.dumps({
                    "id": it.get("myDeviceId"), "nick": it.get("myDeviceNickName"),
                    "bleMac": it.get("myDeviceBleMacAddr"),
                    "regDeviceAppKey": it.get("regDeviceAppKey"),
                    "regWifiAppKey": it.get("regWifiAppKey"),
                    "memberIdx": it.get("memberIdx"),
                }, ensure_ascii=False))
