# JRA Odds Change Monitor

中央競馬の単勝オッズを監視し、一定以上の変化を検知するFlaskアプリです。

## Local Run

```powershell
py -m pip install -r requirements.txt
py app.py
```

Open http://localhost:5000

## Deploy

Start command:

```bash
gunicorn app:app --workers 1 --threads 8 --timeout 120
```

The app reads the `PORT` environment variable automatically on hosting services.

## Notes

This app fetches race and odds pages from netkeiba. Use reasonable intervals and check the target site's terms before public or heavy use.
