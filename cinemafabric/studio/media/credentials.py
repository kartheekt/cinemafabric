"""C2PA Content Credentials: a signed record inside each film and poster saying it was made with AI, and by what.

Uses CF_C2PA_CERT and CF_C2PA_KEY (PEM files) when set. Otherwise a local certificate chain is made once in the data
directory; files signed with it verify as intact, but viewers will say the signer isn't on a public trust list.
"""

import datetime as dt
import json
import os
import tempfile
import threading
from pathlib import Path

from ...config import Settings
from .provenance import LABEL, SOURCE_TYPE, TOOL

MIME = {".mp4": "video/mp4", ".png": "image/png", ".jpg": "image/jpeg"}
_making = threading.Lock()


def available() -> bool:
    try:
        import c2pa  # noqa: F401
    except ImportError:
        return False
    return True


def _write(path: Path, data: bytes, private: bool = False) -> None:
    """Write to a temporary file beside the target, then swap it in, so a crash never leaves half a file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        if private:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _matches(chain: bytes, key: bytes) -> bool:
    """Whether the signing certificate at the head of the chain belongs to this private key."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    try:
        public = x509.load_pem_x509_certificates(chain)[0].public_key()
        private = serialization.load_pem_private_key(key, password=None)
    except Exception:
        return False
    return public.public_numbers() == private.public_key().public_numbers()


def local_chain(folder: Path) -> tuple[bytes, bytes]:
    """A root and a signing certificate kept in the data directory, made the first time they're needed, and made again
    if the stored pair is damaged or doesn't match."""
    cert, key = folder / "c2pa_chain.pem", folder / "c2pa_key.pem"
    with _making:
        if cert.exists() and key.exists():
            chain, pem = cert.read_bytes(), key.read_bytes()
            if _matches(chain, pem):
                os.chmod(key, 0o600)
                return chain, pem
        chain, pem = _new_chain()
        folder.mkdir(parents=True, exist_ok=True)
        _write(key, pem, private=True)
        _write(cert, chain)
        return chain, pem


def _new_chain() -> tuple[bytes, bytes]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    def name(cn: str) -> x509.Name:
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn), x509.NameAttribute(NameOID.ORGANIZATION_NAME, TOOL)])

    now = dt.datetime.now(dt.UTC)
    root_key, sign_key = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    root = (x509.CertificateBuilder().subject_name(name(f"{TOOL} local root")).issuer_name(name(f"{TOOL} local root"))
            .public_key(root_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
            .sign(root_key, hashes.SHA256()))
    leaf = (x509.CertificateBuilder().subject_name(name(f"{TOOL} studio")).issuer_name(root.subject)
            .public_key(sign_key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=825))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
            .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.EMAIL_PROTECTION]), False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(root_key.public_key()), False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(sign_key.public_key()), False)
            .sign(root_key, hashes.SHA256()))
    chain = leaf.public_bytes(serialization.Encoding.PEM) + root.public_bytes(serialization.Encoding.PEM)
    pem = sign_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                 serialization.NoEncryption())
    return chain, pem


def credentials(settings: Settings) -> tuple[bytes, bytes, bool]:
    """(certificate chain, private key, whether it's your own certificate rather than the local one)."""
    if settings.c2pa_cert and settings.c2pa_key:
        return Path(settings.c2pa_cert).read_bytes(), Path(settings.c2pa_key).read_bytes(), True
    chain, pem = local_chain(settings.path("c2pa"))
    return chain, pem, False


def sign(path: Path, title: str, settings: Settings) -> bool:
    """Embed signed Content Credentials in the file, in place. Returns whether the signer is your own certificate."""
    import c2pa

    chain, key, own = credentials(settings)
    manifest = {
        "claim_generator_info": [{"name": TOOL, "version": "0.1"}],
        "title": title,
        "assertions": [
            {"label": "c2pa.actions", "data": {"actions": [
                {"action": "c2pa.created", "digitalSourceType": SOURCE_TYPE, "softwareAgent": {"name": TOOL}},
            ]}},
            {"label": "stds.schema-org.CreativeWork", "data": {
                "@context": "https://schema.org", "@type": "CreativeWork", "name": title, "description": LABEL,
            }},
        ],
    }
    signer = c2pa.Signer.from_info(c2pa.C2paSignerInfo(alg=b"es256", sign_cert=chain, private_key=key, ta_url=None))
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=path.suffix)
    os.close(fd)
    out = Path(tmp)
    try:
        with c2pa.Builder(json.dumps(manifest)) as builder, open(path, "rb") as src, open(out, "w+b") as dst:
            builder.sign(signer, MIME[path.suffix.lower()], src, dst)
        out.replace(path)
    finally:
        out.unlink(missing_ok=True)
    return own


def read(path: Path) -> dict | None:
    """The file's Content Credentials and their validation state, or None if it has none."""
    import c2pa

    try:
        with open(path, "rb") as f, c2pa.Reader(MIME[path.suffix.lower()], f) as reader:
            data = json.loads(reader.json())
    except Exception:
        return None
    codes = [v.get("code") for v in data.get("validation_status", [])]
    return {"state": data.get("validation_state"), "issues": codes, "manifest": data.get("active_manifest")}
