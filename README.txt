Samadhi Receipt Generator

Files:
- app.py
- settings.json
- assets/logo.png
- fonts/Signature.ttf  (add your signature font here if you want cursive signature)

Local testing:
1. Create .streamlit/secrets.toml with:
   APPS_SCRIPT_URL = "YOUR_APPS_SCRIPT_WEBAPP_URL"
   APPS_SCRIPT_TOKEN = "YOUR_SECRET_TOKEN"

2. Put your logo file at:
   assets/logo.png

3. Optionally put your signature font at:
   fonts/Signature.ttf

4. Install packages:
   pip install -r requirements.txt

5. Run:
   streamlit run app.py

Notes:
- This version keeps your existing settings.json values, including trailing spaces.
- It keeps the status field and adds audit helper fields:
  void_reason, reissue_of_receipt_id, replaced_by_receipt_id
- Auto-email is skipped when status is VOIDED.
- Your Apps Script must support:
  next_receipt_id
  upload_pdf
  append_receipt
  send_receipt_email
