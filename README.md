# SEO Audit Tool

A dependency-free Python web app that scans a URL for technical SEO, heading structure, metadata, broken links, robots.txt, sitemap availability, and speed suggestions. It includes tabbed results, PDF download, and optional email delivery.

## Run

```powershell
python app.py
```

Open `http://127.0.0.1:8010` in your browser.

## Optional PageSpeed Insights

Set a Google PageSpeed Insights API key before starting the app:

```powershell
$env:PAGESPEED_API_KEY="your_api_key"
python app.py
```

Without an API key, the app still runs local speed checks for response time, HTML size, compression, caching, and content type.

## Optional Email Sending

Set SMTP details before starting the app:

```powershell
$env:SMTP_HOST="smtp.example.com"
$env:SMTP_PORT="587"
$env:SMTP_USERNAME="user@example.com"
$env:SMTP_PASSWORD="your_password"
$env:SMTP_FROM="user@example.com"
python app.py
```

The email option attaches the generated PDF report.
