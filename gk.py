import sys, json, urllib.request, urllib.error
BASE = "https://www.smartsolity.com"
email = sys.argv[1]; hashed = sys.argv[2]
ptoken = sys.argv[3] if len(sys.argv) > 3 else "ha-solity"
def call(path, body, headers):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE+path, data=data, headers=headers, method=("POST" if data else "GET"))
    try:
        r = urllib.request.urlopen(req, timeout=30)
        return r.status, r.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8","replace")
lb = {"emailId": email, "hashedPwd": hashed, "phoneToken": ptoken, "appSource":"0", "lang":"0"}
st, txt = call("/api_v2/login", lb, {"Content-Type":"application/json"})
d = json.loads(txt); c = d.get("contents") or {}
print("LOGIN", st, "ptoken", ptoken)
acct = {k: c.get(k) for k in c if k not in ("token","tokenPwd")}
print("LOGIN_CONTENTS", json.dumps(acct, ensure_ascii=False)[:1200])
tok = c.get("token"); tokpwd = c.get("tokenPwd")
if not tok:
    print("NO_TOKEN"); sys.exit(0)
h = {"Content-Type":"application/json","Authorization":tok}
if tokpwd: h["AuthorizationPwd"] = tokpwd
st2, txt2 = call("/api_v2/myDevice", None, h)
dd = json.loads(txt2); cc = dd.get("contents") or {}
print("MYDEVICE", st2, "RAW", json.dumps(cc, ensure_ascii=False)[:2500])
