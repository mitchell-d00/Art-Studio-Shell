# Local Art Studio

A local shell-launched art app: prompt dock, style chips, multi-image grid,
lightbox, and save-to-disk. Talks to the OpenAI Images API (or any compatible
`/v1/images/generations` endpoint).

## Run

```bash
cd art_studio
export OPENAI_API_KEY="sk-..."   # optional; you can also paste it in the UI
python3 server.py
```

Then open the printed URL (default http://127.0.0.1:8765).

The key stays in memory on the local server. It is never written to disk.
Generated files land in `outputs/`.

## Models

| Model | Sizes | Quality | Count | Format / background |
|---|---|---|---|---|
| `gpt-image-1`, `gpt-image-1-mini` | 1024x1024, 1536x1024, 1024x1536, auto | low, medium, high, auto | 1–4 | png / jpeg / webp; auto / transparent / opaque |
| `dall-e-3` | 1024x1024, 1792x1024, 1024x1792 | standard, hd | 1 | png only |

The UI swaps the size, quality, and count options when you change model, and
the server validates the same rules before calling the API, so a bad combination
returns a clear error instead of an upstream 400. Transparent backgrounds need
PNG or WebP.

## Notes

- You can start another generation while earlier ones are still running; each
  one gets its own placeholder cards. The header shows how many are in flight.
- Failed generations leave a card with the error and a "Try again" button.
- Token usage from the API shows on each image card when the API reports it.
- No image-edit / mask tools in this cut — generator only.

## Security

The server only accepts requests meant for it:

- POSTs must be `Content-Type: application/json` and, if the browser sends an
  `Origin`, it must match the server's own. This stops other websites you visit
  from quietly spending your credits or replacing your key.
- When bound to loopback (the default), the `Host` header must be
  `127.0.0.1`, `localhost`, or `[::1]` on the right port, which blocks
  DNS-rebinding attacks.

If you set `ART_STUDIO_HOST=0.0.0.0` to use it from another device, anyone who
can reach the port can generate with your key. The server prints a warning when
you do this. Only do it on a network you trust.

## Environment

| Variable | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | (empty) | Key to use at startup |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Any compatible images endpoint |
| `ART_STUDIO_HOST` | `127.0.0.1` | Bind address |
| `ART_STUDIO_PORT` | `8765` | Port |
| `ART_STUDIO_NO_BROWSER` | (unset) | Set to `1` to skip opening a browser |
