import sys, json, urllib.request, urllib.error
BASE = "https://www.smartsolity.com"
email = sys.argv[1]; hashed = sys.argv[2]; dev = sys.argv[3]
def call(method, path, body, headers):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE+path, data=data, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=30); return r.status, r.read().decode("utf-8","replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8","replace")
st, txt = call("POST","/api_v2/login", {"emailId":email,"hashedPwd":hashed,"phoneToken":"ha-solity","appSource":"0","lang":"0"}, {"Content-Type":"application/json"})
c = (json.loads(txt).get("contents") or {})
tok = c.get("token"); tokpwd = c.get("tokenPwd")
h = {"Content-Type":"application/json","Authorization":tok}
if tokpwd: h["AuthorizationPwd"] = tokpwd
# probe candidate endpoints
probes = [
    ("GET", "/api_v2/myDevice/"+dev, None),
    ("GET", "/api_v2/device/"+dev, None),
    ("GET", "/api_v2/shareDevice", None),
    ("GET", "/api_v2/inviteDevice", None),
    ("GET", "/api_v2/myInviteDevice", None),
    ("PUT", "/api_v2/controlDevice/"+dev, {"controlType":"get_status","optionValue":""}),
]
for m,p,b in probes:
    s,t = call(m,p,b,h)
    low = t.lower()
    hit = "KEY!" if ("appkey" in low or "regdevice" in low) else ""
    print(m, p, s, hit, "|", t[:400])
