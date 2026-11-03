"""Optional S3-compatible copy of finished films, so several studio instances can serve the same media."""

import logging
from pathlib import Path

from starlette.exceptions import HTTPException
from starlette.responses import RedirectResponse
from starlette.staticfiles import StaticFiles

from ...config import Settings

log = logging.getLogger("cinemafabric.storage")

KEEP = ("*.mp4", "*.png", "*.srt", "*.vtt", "*.md", "*.json", "*.txt", "*.zip", "frames/*.png", "cast/*.png", "masks/*.png")
TYPES = {".mp4": "video/mp4", ".png": "image/png", ".srt": "application/x-subrip", ".vtt": "text/vtt",
         ".md": "text/markdown", ".json": "application/json", ".txt": "text/plain", ".zip": "application/zip"}


class MediaStore:
    def __init__(self, settings: Settings):
        import boto3

        self.bucket = settings.media_bucket
        self.client = boto3.client("s3", endpoint_url=settings.s3_endpoint or None)

    def upload_job(self, folder: Path, job_id: str) -> int:
        count = 0
        for pattern in KEEP:
            for path in folder.glob(pattern):
                key = f"jobs/{job_id}/{path.relative_to(folder).as_posix()}"
                self.client.upload_file(str(path), self.bucket, key,
                                        ExtraArgs={"ContentType": TYPES.get(path.suffix, "application/octet-stream")})
                count += 1
        return count

    def link(self, key: str, seconds: int = 3600) -> str:
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key},
                                                  ExpiresIn=seconds)


def media_store(settings: Settings) -> MediaStore | None:
    return MediaStore(settings) if settings.media_bucket else None


class MediaFiles(StaticFiles):
    """Local files first (with range requests for video); anything missing comes from the bucket if there is one."""

    def __init__(self, *args, store: MediaStore | None = None, prefix: str = "jobs", **kwargs):
        super().__init__(*args, **kwargs)
        self.store = store
        self.prefix = prefix

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except HTTPException as e:
            key = path.replace("\\", "/")
            if e.status_code != 404 or self.store is None or ".." in key.split("/"):
                raise
            return RedirectResponse(self.store.link(f"{self.prefix}/{key}"), status_code=307)
