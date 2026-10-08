"""Schlanker S3-Client ohne Zusatzpakete (Signatur V4, Multipart, Range-Download, signierte Links).

Läuft gegen Amazon S3 und S3-kompatible Speicher (NetApp StorageGRID, MinIO, Ceph, Wasabi, Synology …).
Eigene Endpunkte nutzen Pfad-Adressierung (https://host/bucket/key), Amazon die virtuelle (bucket.s3.region…).
Selbst signierte Zertifikate: ca_file (eigene Zertifizierungsstelle) oder verify=False.
"""
import base64
import datetime
import hashlib
import hmac
import http.client
import os
import ssl
import threading
import time
import urllib.parse
import xml.etree.ElementTree as ET

# Signatur V2 (ältere S3-Zugänge, z. B. StorageGRID-Konten, die im S3 Browser mit "Signature V2" laufen):
# diese Query-Parameter gehören in die signierte Ressource
V2_SUBRESOURCES = {"acl", "cors", "delete", "lifecycle", "location", "logging", "notification", "partNumber", "policy",
                   "requestPayment", "restore", "tagging", "torrent", "uploadId", "uploads", "versionId", "versioning",
                   "versions", "website", "response-content-type", "response-content-language", "response-expires",
                   "response-cache-control", "response-content-disposition", "response-content-encoding"}
MIN_PART = 8 * 1024 * 1024
MAX_PARTS = 10000


class S3Error(Exception):
    def __init__(self, status, code, message, key="", body=b""):
        super().__init__("%s %s: %s%s" % (status, code, message, (" (%s)" % key) if key else ""))
        self.status, self.code, self.body = status, code, body


def _q(s, safe="-_.~"):
    return urllib.parse.quote(s, safe=safe)


def cert_names(host, port=443, timeout=8):
    """Namen im Zertifikat eines Servers (ohne Prüfung gelesen) – für verständliche Fehlermeldungen."""
    import socket
    import tempfile

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with socket.create_connection((host, port), timeout=timeout) as s:
        with ctx.wrap_socket(s, server_hostname=host) as t:
            der = t.getpeercert(binary_form=True)
    fd, f = tempfile.mkstemp(suffix=".pem")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(ssl.DER_cert_to_PEM_cert(der))
        d = ssl._ssl._test_decode_cert(f)
    finally:
        os.remove(f)
    names = [v for k, v in d.get("subjectAltName", ()) if k == "DNS"]
    return names or [v for rdn in d.get("subject", ()) for k, v in rdn if k == "commonName"]


def _strip_ns(root):
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


class Client:
    def __init__(self, endpoint="", region="", bucket="", access_key="", secret_key="", path_style=None,
                 verify=True, ca_file="", timeout=120, sig="v4"):
        self.sig = "v2" if str(sig).lower() == "v2" else "v4"
        endpoint = (endpoint or "").strip().rstrip("/")
        self.region = (region or "").strip() or "us-east-1"
        if not endpoint:
            endpoint = "https://s3.%s.amazonaws.com" % self.region
        if "://" not in endpoint:
            endpoint = "https://" + endpoint
        u = urllib.parse.urlsplit(endpoint)
        self.scheme, self.host, self.base_path = u.scheme, u.netloc, u.path.rstrip("/")
        self.bucket = bucket.strip()
        self.ak, self.sk = access_key.strip(), secret_key.strip()
        aws = self.host.endswith("amazonaws.com")
        self.path_style = (not aws) if path_style is None else bool(path_style)
        if aws and not self.path_style and "." in self.bucket:
            self.path_style = True  # Punkte im Bucketnamen passen nicht zum Zertifikat
        self.timeout = timeout
        self.ctx = None
        if self.scheme == "https":
            self.ctx = ssl.create_default_context(cafile=ca_file or None)
            if verify == "chain":  # Zertifikat muss gültig sein, darf aber auf einen anderen Namen lauten
                self.ctx.check_hostname = False
            elif not verify:
                self.ctx.check_hostname = False
                self.ctx.verify_mode = ssl.CERT_NONE
        self._local = threading.local()

    # ------------------------------------------------------------ Grundlagen --
    def _host_path(self, key):
        if self.path_style:
            path = self.base_path + "/" + _q(self.bucket) + ("/" + _q(key, "-_.~/") if key else "/")
            return self.host, path
        return self.bucket + "." + self.host, self.base_path + "/" + _q(key, "-_.~/")

    def _conn(self, host):
        c = getattr(self._local, "conn", None)
        if c is None or getattr(self._local, "chost", None) != host:
            if c:
                c.close()
            cls = http.client.HTTPSConnection if self.scheme == "https" else http.client.HTTPConnection
            kw = {"context": self.ctx} if self.scheme == "https" else {}
            c = self._local.conn = cls(host, timeout=self.timeout, **kw)
            self._local.chost = host
        return c

    def _drop_conn(self):
        c = getattr(self._local, "conn", None)
        if c:
            try:
                c.close()
            except Exception:
                pass
        self._local.conn = None

    def _sign(self, method, host, path, query, headers, payload_hash, now=None):
        now = now or datetime.datetime.now(datetime.timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        day = amzdate[:8]
        headers = dict(headers)
        headers["host"] = host
        headers["x-amz-date"] = amzdate
        headers["x-amz-content-sha256"] = payload_hash
        canon_q = "&".join("%s=%s" % (_q(k), _q(v)) for k, v in sorted(query.items()))
        names = sorted(k.lower() for k in headers)
        lower = {k.lower(): " ".join(str(v).strip().split()) for k, v in headers.items()}
        canon_h = "".join("%s:%s\n" % (n, lower[n]) for n in names)
        signed = ";".join(names)
        creq = "\n".join([method, path, canon_q, canon_h, signed, payload_hash])
        scope = "%s/%s/s3/aws4_request" % (day, self.region)
        sts = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope, hashlib.sha256(creq.encode()).hexdigest()])
        self._local.last = (creq, sts)  # für die Fehlersuche bei SignatureDoesNotMatch
        k = ("AWS4" + self.sk).encode()
        for part in (day, self.region, "s3", "aws4_request"):
            k = hmac.new(k, part.encode(), hashlib.sha256).digest()
        sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
        headers["Authorization"] = "AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s" % (
            self.ak, scope, signed, sig)
        return headers

    def _auth_path(self, key):
        """Pfad für die V2-Signatur: immer /bucket/key (auch bei virtueller Adressierung)."""
        return self.base_path + "/" + _q(self.bucket) + ("/" + _q(key, "-_.~/") if key else "/") \
            if not self.path_style else self._host_path(key)[1]

    def _sign_v2(self, method, key, query, headers, now=None):
        import email.utils

        headers = dict(headers)
        lower = {k.lower(): str(v).strip() for k, v in headers.items()}
        date = email.utils.formatdate(now, usegmt=True) if now is not None else email.utils.formatdate(usegmt=True)
        headers["Date"] = date
        amz = "".join("%s:%s\n" % (k, lower[k]) for k in sorted(k for k in lower if k.startswith("x-amz-")))
        res = self._auth_path(key)
        sub = sorted((k, v) for k, v in query.items() if k in V2_SUBRESOURCES)
        if sub:
            res += "?" + "&".join(k + ("=" + v if v != "" else "") for k, v in sub)
        sts = "%s\n%s\n%s\n%s\n%s%s" % (method, lower.get("content-md5", ""), lower.get("content-type", ""), date, amz, res)
        sig = base64.b64encode(hmac.new(self.sk.encode(), sts.encode(), hashlib.sha1).digest()).decode()
        headers["Authorization"] = "AWS %s:%s" % (self.ak, sig)
        self._local.last = (sts, sts)
        return headers

    def request(self, method, key="", query=None, headers=None, body=b"", stream=False, ok=(200, 204, 206),
                retries=5):
        """Eine Anfrage mit Wiederholung bei Netzfehlern und 5xx/SlowDown. body: bytes."""
        query = {k: ("" if v is None else str(v)) for k, v in (query or {}).items()}
        host, path = self._host_path(key)
        # leere Parameter ohne "=" (z. B. ?uploads) – so erwartet es die V2-Signatur, V4 ist das egal
        url = path + ("?" + "&".join(_q(k) + ("=" + _q(v) if v != "" else "") for k, v in sorted(query.items()))
                      if query else "")
        payload_hash = hashlib.sha256(body).hexdigest() if self.sig == "v4" else ""
        delay = 1.0
        for attempt in range(retries + 1):
            if self.sig == "v2":
                hdrs = self._sign_v2(method, key, query, headers or {})
            else:
                hdrs = self._sign(method, host, path, query, headers or {}, payload_hash)
            if body:
                hdrs["Content-Length"] = str(len(body))
            elif method in ("PUT", "POST"):
                hdrs["Content-Length"] = "0"
            try:
                c = self._conn(host)
                c.request(method, url, body=body or None, headers=hdrs)
                r = c.getresponse()
            except (OSError, http.client.HTTPException) as ex:
                self._drop_conn()
                if attempt == retries:
                    raise S3Error(0, "Netzwerk", str(ex) or type(ex).__name__, key)
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            if r.status in ok:
                if stream:
                    return r
                data = r.read()
                return r, data
            data = r.read()
            code, msg = "", ""
            try:
                x = _strip_ns(ET.fromstring(data))
                code, msg = x.findtext("Code") or "", x.findtext("Message") or ""
            except ET.ParseError:
                msg = data[:200].decode("utf-8", "replace")
            if (r.status >= 500 or code in ("SlowDown", "RequestTimeout", "RequestTimeTooSkewed")) and attempt < retries:
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise S3Error(r.status, code or r.reason, msg or r.reason, key, data)

    # ------------------------------------------------------------- Bucket --
    def last_signed(self):
        return getattr(self._local, "last", None)

    def check(self):
        """Verbindung prüfen: Bucket auflisten (1 Eintrag). Gibt True oder wirft S3Error."""
        self.request("GET", "", {"list-type": "2", "max-keys": "1"})
        return True

    def list(self, prefix="", delimiter=None):
        """Alle Objekte (key, size, etag, last_modified) unter prefix; mit delimiter auch ("prefix", name)."""
        token = None
        while True:
            q = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if delimiter:
                q["delimiter"] = delimiter
            if token:
                q["continuation-token"] = token
            _r, data = self.request("GET", "", q)
            x = _strip_ns(ET.fromstring(data))
            for c in x.findall("Contents"):
                yield (c.findtext("Key"), int(c.findtext("Size") or 0), (c.findtext("ETag") or "").strip('"'),
                       c.findtext("LastModified"))
            for p in x.findall("CommonPrefixes"):
                yield ("prefix", p.findtext("Prefix"))
            if x.findtext("IsTruncated") != "true":
                return
            token = x.findtext("NextContinuationToken")
            if not token:
                return

    def head(self, key):
        try:
            r, _ = self.request("HEAD", key)
        except S3Error as ex:
            if ex.status == 404:
                return None
            raise
        return {k.lower(): v for k, v in r.getheaders()}

    def delete(self, key):
        self.request("DELETE", key)

    def copy(self, src_key, dst_key):
        """Serverseitige Kopie (bis 5 GB) – nichts wird erneut übertragen."""
        _r, data = self.request("PUT", dst_key, headers={"x-amz-copy-source": "/%s/%s" % (
            _q(self.bucket), _q(src_key, "-_.~/"))})
        x = _strip_ns(ET.fromstring(data))
        if x.tag == "Error":
            raise S3Error(200, x.findtext("Code"), x.findtext("Message"), dst_key)

    def get_policy(self):
        """Bucket-Richtlinie (Text) oder None, wenn keine gesetzt ist."""
        try:
            _r, data = self.request("GET", "", {"policy": ""})
        except S3Error as ex:
            if ex.status == 404:
                return None
            raise
        return data.decode("utf-8", "replace")

    def put_policy(self, policy_json):
        self.request("PUT", "", {"policy": ""}, {"Content-Type": "application/json"}, policy_json.encode())

    # ------------------------------------------------------------- Upload --
    @staticmethod
    def _md5(b):
        return base64.b64encode(hashlib.md5(b).digest()).decode()

    def put(self, key, data, headers=None):
        h = dict(headers or {})
        h["Content-MD5"] = self._md5(data)
        r, _ = self.request("PUT", key, headers=h, body=data)
        return r.getheader("ETag", "").strip('"')

    def mp_create(self, key, headers=None):
        _r, data = self.request("POST", key, {"uploads": ""}, headers or {})
        return _strip_ns(ET.fromstring(data)).findtext("UploadId")

    def mp_part(self, key, upload_id, n, data):
        r, _ = self.request("PUT", key, {"partNumber": n, "uploadId": upload_id}, {"Content-MD5": self._md5(data)},
                            data)
        return r.getheader("ETag", "").strip('"')

    def mp_parts(self, key, upload_id):
        """Schon hochgeladene Teile {Nummer: (etag, size)} – zum Fortsetzen."""
        out, marker = {}, None
        while True:
            q = {"uploadId": upload_id}
            if marker:
                q["part-number-marker"] = marker
            _r, data = self.request("GET", key, q)
            x = _strip_ns(ET.fromstring(data))
            for p in x.findall("Part"):
                out[int(p.findtext("PartNumber"))] = ((p.findtext("ETag") or "").strip('"'), int(p.findtext("Size") or 0))
            if x.findtext("IsTruncated") != "true":
                return out
            marker = x.findtext("NextPartNumberMarker")

    def mp_complete(self, key, upload_id, parts):
        body = "<CompleteMultipartUpload>" + "".join(
            "<Part><PartNumber>%d</PartNumber><ETag>\"%s\"</ETag></Part>" % (n, e) for n, e in sorted(parts)) + \
            "</CompleteMultipartUpload>"
        _r, data = self.request("POST", key, {"uploadId": upload_id}, {"Content-Type": "application/xml"},
                                body.encode())
        x = _strip_ns(ET.fromstring(data))
        if x.tag == "Error":  # S3 kann 200 mit Fehler im Text liefern
            raise S3Error(200, x.findtext("Code"), x.findtext("Message"), key)
        return (x.findtext("ETag") or "").strip('"')

    def mp_abort(self, key, upload_id):
        try:
            self.request("DELETE", key, {"uploadId": upload_id})
        except S3Error as ex:
            if ex.status != 404:
                raise

    @staticmethod
    def part_size(size):
        ps = MIN_PART
        while ps * MAX_PARTS < size:
            ps *= 2
        return ps

    # ----------------------------------------------------------- Download --
    def get_to_file(self, key, dest, size=None, progress=None, chunk=1024 * 1024):
        """Objekt nach dest laden; unterbrochene Downloads (dest + ".fa-part") werden per Range fortgesetzt."""
        part = dest + ".fa-part"
        have = os.path.getsize(part) if os.path.exists(part) else 0
        if size is not None and have > size:
            have = 0
        headers = {"Range": "bytes=%d-" % have} if have else {}
        if size is not None and have == size and size > 0:
            os.replace(part, dest)
            return size
        r = self.request("GET", key, headers=headers, stream=True)
        if r.status == 200:
            have = 0  # Server kann Range ignorieren
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        try:
            with open(part, "ab" if have else "wb") as f:
                while True:
                    b = r.read(chunk)
                    if not b:
                        break
                    f.write(b)
                    have += len(b)
                    if progress:
                        progress(len(b))
        except (OSError, http.client.HTTPException):
            self._drop_conn()
            raise
        if size is not None and have != size:
            raise S3Error(0, "Unvollständig", "%d von %d Bytes" % (have, size), key)
        os.replace(part, dest)
        return have

    # ------------------------------------------------------ signierte Links --
    def presign(self, key, expires=7 * 24 * 3600, now=None):
        if self.sig == "v2":  # V2: Ablaufzeit als Zeitstempel
            exp = str(int((now.timestamp() if now else time.time()) + expires))
            sts = "GET\n\n\n%s\n%s" % (exp, self._auth_path(key))
            sig = base64.b64encode(hmac.new(self.sk.encode(), sts.encode(), hashlib.sha1).digest()).decode()
            host, path = self._host_path(key)
            return "%s://%s%s?AWSAccessKeyId=%s&Expires=%s&Signature=%s" % (
                self.scheme, host, path, _q(self.ak), exp, _q(sig))
        now = now or datetime.datetime.now(datetime.timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        scope = "%s/%s/s3/aws4_request" % (amzdate[:8], self.region)
        host, path = self._host_path(key)
        q = {"X-Amz-Algorithm": "AWS4-HMAC-SHA256", "X-Amz-Credential": "%s/%s" % (self.ak, scope),
             "X-Amz-Date": amzdate, "X-Amz-Expires": str(int(min(expires, 7 * 24 * 3600))),
             "X-Amz-SignedHeaders": "host"}
        canon_q = "&".join("%s=%s" % (_q(k), _q(v)) for k, v in sorted(q.items()))
        creq = "\n".join(["GET", path, canon_q, "host:%s\n" % host, "host", "UNSIGNED-PAYLOAD"])
        sts = "\n".join(["AWS4-HMAC-SHA256", amzdate, scope, hashlib.sha256(creq.encode()).hexdigest()])
        k = ("AWS4" + self.sk).encode()
        for p in (amzdate[:8], self.region, "s3", "aws4_request"):
            k = hmac.new(k, p.encode(), hashlib.sha256).digest()
        sig = hmac.new(k, sts.encode(), hashlib.sha256).hexdigest()
        return "%s://%s%s?%s&X-Amz-Signature=%s" % (self.scheme, host, path, canon_q, sig)

    def public_url(self, key):
        host, path = self._host_path(key)
        return "%s://%s%s" % (self.scheme, host, path)
