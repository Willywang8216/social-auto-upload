# Media storage publishing pipeline

URL-fetch platforms need a public HTTPS media URL. The media storage dispatcher tries share hosting, then S3-compatible object storage, then the legacy rclone backend. If no backend can produce a usable URL, publishing should stop with a clear error instead of using a localhost URL.

Production canary checklist:

- Public artifact URL returns HTTP 200 from outside the container.
- Video artifacts return an expected content type such as video/mp4.
- The URL is not localhost or a private network address.
- TikTok Direct Post public URLs match the configured verified URL prefixes.

Meta and Threads still require reconnecting accounts when permissions are stale. TikTok draft upload can work without domain verification when file upload mode is used; public Direct Post requires a verified public media domain.
