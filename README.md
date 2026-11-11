# WhatsApp Order Status Assistant

This package runs the order tracker, WhatsApp dashboard, and Festive Agent as separate local programs. WhatsApp order questions are handled by a LangGraph agent using the model configured by `GEMINI_MODEL`. The agent calls the `get_order` MCP tool for order data. Requests for human support are summarized and sent to the Festive Agent dashboard inbox.

## Setup

Use Python 3.11 or 3.12. Keep your API keys and WhatsApp Meta credentials private.

1. In `order_status_tracker`, create `.env` from `.env.example` and set a long random `ORDER_API_KEY`.
2. Install and start the order API:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
   ```

3. In `whatsapp_chat_dashboard`, create `.env` from `.env.example`. Add a Gemini API key from Google AI Studio as `GEMINI_API_KEY`, keep `GEMINI_MODEL=gemini-3.8-flash`, set the same order `ORDER_API_KEY`, and add your existing Meta values.
4. Start the Festive Agent on port 8002. This dashboard receives human support requests. Run this from the `combined_order_whatsapp` folder after installing the order tracker requirements, which include the needed FastAPI packages:

   ```powershell
   .\order_status_tracker\.venv\Scripts\python.exe -m uvicorn app:app --app-dir .\festive_agent --host 127.0.0.1 --port 8002
   ```

5. Install and start the WhatsApp app on port 8001:

   ```powershell
   py -3.12 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   .\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8001
   ```

6. Point your existing HTTPS tunnel and Meta webhook callback at port 8001 `/webhook`. Keep all three local services running. The Festive Agent bell shows unread human support requests; opening one marks it read and displays its transcript, customer number, and detected order ID.

The order API is available at `http://127.0.0.1:8000`; single-order lookups require the `X-API-Key` header. Sample orders are `ORD101` through `ORD110`. No database, virtual environment, or `.env` file is included in this ZIP. Gemini API quota and billing follow Google's current Gemini API terms.


cd .\order_status_tracker
cd .\whatsapp_chat_dashboard



order

py -3.12 -m venv .\combined_order_whatsapp\order_status_tracker\.venv
& .\combined_order_whatsapp\order_status_tracker\.venv\Scripts\python.exe -m pip install -r .\combined_order_whatsapp\order_status_tracker\requirements.txt

order run 
& .\combined_order_whatsapp\order_status_tracker\.venv\Scripts\python.exe -m uvicorn app:app --app-dir .\combined_order_whatsapp\order_status_tracker --host 127.0.0.1 --port 8000


get api key

$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 32
$rng.GetBytes($bytes)
$key = [BitConverter]::ToString($bytes).Replace("-", "").ToLower()
$rng.Dispose()
$key


wahtsapp

.\.venv\Scripts\python.exe -m pip install -r .\combined_order_whatsapp\whatsapp_chat_dashboard\requirements.txt

whatsapp run

order
d:\whatsapp_order_status_gemini_3_8_flash\combined_order_whatsapp\order_status_tracker\.venv\Scripts\python.exe -m uvicorn app:app --app-dir d:\whatsapp_order_status_gemini_3_8_flash\combined_order_whatsapp\order_status_tracker --host 127.0.0.1 --port 8000

whats app
  d:\whatsapp_order_status_gemini_3_8_flash\.venv\Scripts\python.exe -m uvicorn app:app --app-dir d:\whatsapp_order_status_gemini_3_8_flash\combined_order_whatsapp\whatsapp_chat_dashboard --host 127.0.0.1 --port 8001


festive agent
 d:\whatsapp_order_status_gemini_3_8_flash\combined_order_whatsapp\order_status_tracker\.venv\Scripts\python.exe -m uvicorn app:app --app-dir d:\whatsapp_order_status_gemini_3_8_flash\combined_order_whatsapp\festive_agent --host 127.0.0.1 --port 8002  

 webhook
  ngrok http 8001  
