# Order Status Tracker

A small local web app based on the sketch. Choose an order from `ORD101` through `ORD110`, then click the next status: **Shipped**, **On the way**, or **Delivered**. Each click updates the current order and adds a timestamped row to the SQLite status history. Data stays in `orders.db` after the app is closed.

## Run on Windows

Open PowerShell in the folder where you extracted this ZIP and run:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000` in your browser. Keep the PowerShell window open while using the page.

## How it works

- Select an order from the Order ID dropdown.
- Click **Set to Shipped**, then **Set to On the way**, then **Set to Delivered**.
- The next status becomes clickable only after the previous step has been saved.
- The Status history shows every saved step and its time.
- The SQLite database is created beside `app.py` as `orders.db`.

This demo uses local sample orders and does not connect to WhatsApp or the existing retail database.
