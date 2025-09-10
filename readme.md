นี่ครับ **README.md** เวอร์ชัน “พร้อมก๊อป” วางได้เลยที่รากโปรเจ็กต์ 👇

---

# Translator SaltyBot (Discord)

OCR • STT • Translate • TTS — ครบในบอทเดียว

> บอทแปลภาษาใน Discord: อ่านจาก **ข้อความ**, **ภาพ (OCR)**, และ **เสียง (STT)**
> มี **แผงเลือกภาษา STT** ก่อนถอดเสียง, ปุ่ม **ฟังเสียง (TTS)**, และเลือกเอนจินแปลได้ (GPT / Google)

---

## ✨ ไฮไลต์

* **แปลข้อความทันที** ในห้องที่กำหนด (โหมด 2 ทิศทาง หรือโหมด Multi)
* **OCR จากภาพ** → โชว์ข้อความที่ดึงได้ พร้อมปุ่ม “Listen / Translate”
* **STT จากไฟล์เสียง/วิดีโอ**

  * อัปโหลดไฟล์ → บอทถามภาษาพูด **(แผงปุ่มเลือกภาษา)**
  * เมื่อเลือกแล้ว: บอท **ลบแผงเลือกภาษา** และส่ง **Transcript** เป็น **reply** ไปยังไฟล์เดิม
  * รองรับไฟล์ยาว: สลับใช้ **Google Speech-to-Text** แบบ sync / long-running อัตโนมัติ
  * มีตัวช่วยแปลงเป็น **WAV 16k mono** เมื่อจำเป็น (แก้ปัญหา 400 / stereo)
* **TTS ฟังผลลัพธ์** (ปุ่ม Listen) + **Auto TTS channel**
* **สลับเอนจินแปล** ระดับเซิร์ฟเวอร์: `GPT-4o mini`, `GPT-5 nano`, `Google Translate`
* **โควต้า**: ติดตามการใช้ Google Translate / OCR ด้วย Redis
* **UI เป็นมิตร**: ปุ่ม 3 ภาษา + เมนูเลือกภาษาอื่น ๆ, แสดงธง, กันข้อความยาวล้น

---

## 🖼️ หน้าตาฟีเจอร์ (ตัวอย่าง)

* แผงเลือกภาษา STT (ก่อนถอดเสียง)
* ผลลัพธ์ Transcript + ปุ่ม “Listen / Translate”
* แผงแปล 2 ทาง (เลือกภาษาปลายทางแบบปุ่ม/ดรอปดาวน์)

> *(เพิ่มสกรีนช็อตของคุณภายหลังในส่วนนี้)*

---

## 🧩 โครงสร้างไฟล์หลัก

```
.
├─ bot.py / main.py             # จุดรันบอท (เรียก register_message_handlers + bot.run)
├─ events.py                    # Routing เหตุการณ์ on_message + Flow รวม (OCR/STT/Translate)
├─ stt_select_panel.py          # แผงเลือกภาษา STT (ลบแผงหลังเลือก, reply ไปยังไฟล์เดิม)
├─ translate_panel.py           # แผงแปล 2 ทาง + ปุ่มฟัง/ฟังต้นฉบับ
├─ stt_google_sync.py           # Google STT (สั้น)
├─ stt_google_async.py          # Google STT long-running (อัปโหลด GCS + poll)
├─ stt_lang_utils.py            # เดาภาษา/สคริปต์ + เลือก alternative langs
├─ tts_lang_resolver.py         # ตัดอีโมจิ, แยก/รวมบล็อก, เดา TTS lang, cleaning
├─ tts_service.py               # พูด (gTTS/Edge) + คิว + connect เข้าห้องเสียง
├─ ocr_service.py               # Google Vision OCR
├─ translation_service.py       # เอนจินแปล (GPT/Google) + post-process
├─ media_utils.py               # ffmpeg แปลงเสียงให้เข้ากับ STT (WAV 16k mono ฯลฯ)
├─ app_redis.py                 # โควต้า/usage/histogram ภาษา per channel/user
├─ constants.py                 # ตั้งค่า Channel IDs/โหมด/ลิมิต ฯลฯ
├─ config.py                    # ENV / คีย์ต่าง ๆ (ดึงจาก os.environ)
├─ lang_config.py               # ชื่อภาษา/ธงแสดงผล
└─ messaging_utils.py           # helper ส่งข้อความยาว ฯลฯ
```

---

## ⚙️ ความต้องการ

* Python **3.10+**
* **FFmpeg** (จำเป็นสำหรับแปลง/เล่นเสียง)
* Google Cloud + เปิดใช้งาน:

  * Speech-to-Text, **(ถ้าใช้ long-running)** Cloud Storage (GCS), Vision (OCR), Cloud Translation
* Redis (สำหรับโควต้า/usage; ถ้าไม่มีจะขึ้นแจ้งเตือน)
* Discord Bot Token
* OpenAI API Key (ถ้าเลือกใช้ GPT เป็นเอนจินแปล)

---

## 🔑 ตัวแปรแวดล้อม (ENV)

| ตัวแปร                    | อธิบาย                                                                  |
| ------------------------- | ----------------------------------------------------------------------- |
| `DISCORD_BOT_TOKEN`       | โทเคนบอท Discord                                                        |
| `OPENAI_API_KEY`          | (ตัวเลือก) ใช้เมื่อเลือกเอนจินแปล GPT                                   |
| `GOOGLE_API_KEY`          | ใช้เรียก **Vision OCR** และ **Cloud Translation**                       |
| `GCP_SERVICE_ACCOUNT_B64` | เนื้อไฟล์ Service Account (JSON) เข้ารหัส Base64 สำหรับ **STT**/**GCS** |
| `GCS_BUCKET_NAME`         | ชื่อ GCS bucket สำหรับ long-running STT                                 |
| `REDIS_URL`               | URL ของ Redis (เช่น `redis://…`)                                        |

> โค้ดจะถอด `GCP_SERVICE_ACCOUNT_B64` เป็นไฟล์ที่ `/app/gcp-key.json` และตั้ง `GOOGLE_APPLICATION_CREDENTIALS` ให้อัตโนมัติ

---

## 🚀 เริ่มต้นใช้งาน (Local)

1. ติดตั้ง FFmpeg

```bash
# macOS (Homebrew)
brew install ffmpeg

# Ubuntu / Debian
sudo apt-get update && sudo apt-get install -y ffmpeg
```

2. ติดตั้งไลบรารี Python

```bash
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

3. ตั้งค่า ENV (ตัวอย่าง `.env`)

```env
DISCORD_BOT_TOKEN=xxx
OPENAI_API_KEY=xxx
GOOGLE_API_KEY=xxx
GCP_SERVICE_ACCOUNT_B64=...base64-of-service-account-json...
GCS_BUCKET_NAME=your-bucket-name
REDIS_URL=redis://localhost:6379/0
```

4. รันบอท

```bash
python bot.py
# หรือ
python main.py
```

---

## 🔧 ตั้งค่าใน Discord

* เปิด **Message Content Intent** และ **Server Members Intent** ในหน้า Developer Portal
* เชิญบอทเข้ากิลด์ด้วยสิทธิ์:

  * ส่งข้อความ, อ่านประวัติ, ฝังลิงก์/ฝัง embed
  * จัดการข้อความ (ถ้าจะใช้คำสั่ง `!clear`)
  * เข้าร่วม/พูดใน **Voice Channel** (สำหรับ TTS)

---

## 🗂️ ตั้งค่าห้อง/โหมด (ใน `constants.py`)

* `TRANSLATION_CHANNELS`

  * `multi` → โหมดหลายภาษา: มีแผงแปล 2 ทาง (ข้อความ) + รองรับ OCR/STT
  * `(src, tgt)` → โหมดสองทิศทาง: ถ้าพิมพ์ `src` จะแปลไป `tgt` และกลับกัน
* `AUTO_TTS_CHANNELS` → ห้องที่พิมพ์แล้วบอทอ่านออกเสียงอัตโนมัติ
* `DETAILED_EN_CHANNELS` / `DETAILED_JA_CHANNELS` → ห้องวิเคราะห์เชิงลึก

> แก้ไข ID ห้องให้ตรงกับเซิร์ฟเวอร์ของคุณ จากนั้นรีสตาร์ตบอท

---

## 🕹️ วิธีใช้ (คำสั่ง)

พิมพ์ `!commands` ในห้องใดก็ได้เพื่อดูรายการสรุป

* `!tts engine [user|server] [gtts|edge]` — ตั้งค่า TTS
* `!ttsstatus` — ดูสถานะ TTS ปัจจุบัน
* `!translator engine [gpt4omini|gpt5nano|google]` — ตั้งค่าเอนจินแปล
* `!translator show` / `!translatorstatus` — ดูเอนจินแปลที่ใช้งาน
* `!ocr quota` — เช็คโควต้า OCR วันนี้
* `!gtrans quota` — เช็คโควต้า Google Translate ทั้งบอท
* `!topusers` — อันดับการใช้งานบอทในเซิร์ฟเวอร์
* `!clear [จำนวน]` — ลบข้อความล่าสุด (ต้องมีสิทธิ์)

---

## 🔄 โฟลว์การใช้งานหลัก

### 1) STT (ถอดเสียงจากไฟล์)

1. อัปโหลดไฟล์เสียง/วิดีโอในห้องโหมด `multi`
2. บอทส่ง **แผงเลือกภาษา** → กดเลือก
3. บอท **ลบแผงเลือกภาษาออก** และส่ง **Transcript** เป็น **reply ไปยังไฟล์เดิม**
4. Transcript จะแสดง **ภาษาที่เลือก** และโหมด (เช่น `google sync`/`google longrunning`)

   > *ใน STT จะ **ไม่แสดงชื่อเอนจินแปล** ตามดีไซน์ปัจจุบัน*
5. มีปุ่ม “Listen (Result)” / “Listen (Source)” / “Translate” ให้กดต่อ

> ระบบจะพยายามแปลงไฟล์เป็น **WAV 16k mono** อัตโนมัติถ้าจำเป็น และเลือก alt-langs รอบถัดไปกรณีถอดไม่ออก

### 2) OCR (ดึงข้อความจากภาพ)

1. อัปโหลดภาพในห้องโหมด `multi`
2. บอทส่งข้อความที่ดึงได้ + ปุ่ม “Listen / Translate”
3. ถ้าข้อความยาวจะมีลิงก์ไฟล์แนบ **Full TXT**

### 3) แปลข้อความ (Text → Text)

* ห้อง `multi` → เรียก **แผงแปล 2 ทาง** ให้เลือกภาษาปลายทาง
* ห้องปกติ `(src, tgt)` → แปลสองทิศทางอัตโนมัติ

---

## 📦 Deployment Tips

* **Railway / Docker**: ใส่ ENV ให้ครบ + ติดตั้ง `ffmpeg` ใน image/base
* GCS: สร้าง bucket แล้วกำหนด `GCS_BUCKET_NAME`; ใส่ Service Account เป็น Base64 ใน `GCP_SERVICE_ACCOUNT_B64`
* Redis: ใส่ `REDIS_URL` เพื่อเก็บโควต้า/usage/histogram ภาษา

---

## 🛠️ Troubleshooting

* **HTTP 400: “Must use single channel (mono)”**
  ใช้ตัวช่วยในระบบจะบังคับแปลงเป็น **WAV 16k mono** ให้โดยอัตโนมัติ (ผ่าน `media_utils.transcode_to_wav_pcm16`)
* **เสียงยาวมาก**
  ระบบจะสลับเป็น **long-running** ให้อัตโนมัติ (ต้องมี GCS และ Service Account)
* **ข้อความ OCR/แปลยาวเกิน**
  ระบบจะแนบไฟล์ TXT เพิ่มต่างหาก
* **Google Translate quota เต็ม**
  สลับมา GPT ชั่วคราวด้วย `!translator engine gpt4omini` หรือรอวันถัดไป

---

## 🔒 ความปลอดภัย

* เก็บคีย์ต่าง ๆ ผ่าน ENV เท่านั้น
* อย่า commit ไฟล์ Service Account ลงรีโป
* จำกัดสิทธิ์บอทเฉพาะที่จำเป็นในเซิร์ฟเวอร์

---

## ✅ License

ระบุไลเซนส์ของโปรเจ็กต์คุณที่นี่ (เช่น MIT)

---

## 🙏 Credits

* Google Cloud (STT, Vision, Translate)
* OpenAI (Responses API)
* gTTS / Edge-TTS
* discord.py, httpx, ffmpeg

---

## 🧪 เช็กลิสต์ก่อนใช้งานจริง

* [ ] ตั้งค่า Channel IDs ใน `constants.py`
* [ ] ใส่ ENV ครบ (`DISCORD_BOT_TOKEN`, `GOOGLE_API_KEY`, `GCP_SERVICE_ACCOUNT_B64`, …)
* [ ] เปิด Intent ใน Discord Developer Portal
* [ ] ติดตั้ง `ffmpeg`
* [ ] ทดสอบ: ข้อความ / ภาพ (OCR) / เสียง (STT)
* [ ] ตรวจโควต้า `!ocr quota` / `!gtrans quota`

> เจอปัญหา ส่ง log/traceback ล่าสุดมาได้เลยครับ เดี๋ยวช่วยไล่ให้ 🙌

---
