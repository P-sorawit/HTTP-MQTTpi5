# ทดสอบ TopGun HTTP API ในเครื่อง

เซิร์ฟเวอร์นี้จำลอง API ใน `../API_DOCS.md` โดยใช้ภาพจาก Google Drive
ทดสอบการดึงภาพ ส่ง JSON และอ่านสถิติได้โดยไม่แก้ `../client.py`
ไม่มีการคำนวณคะแนนความแม่นยำหรือส่งข้อมูลขึ้น MQTT จริง

## 1. เตรียมภาพและติดตั้ง

เตรียมโฟลเดอร์ Google Drive ที่มีภาพ `.jpg`, `.jpeg`, `.png` (รองรับโฟลเดอร์ย่อย)
ตั้ง Share → General access → **Anyone with the link → Viewer**
ไฟล์และโฟลเดอร์ย่อยทุกอันที่ใช้ต้องเปิดให้อ่านผ่านลิงก์ได้ด้วย
ใช้ชื่อภาพไม่ซ้ำกันแม้อยู่คนละโฟลเดอร์หรือใช้ตัวพิมพ์ต่างกัน
แนะนำชื่อ ASCII เช่น `coffee_01.jpg`: ชื่อภาษาไทยใช้กับ `X-Image-Name`
ของ client เดิมไม่ได้ เซิร์ฟเวอร์จะหยุดพร้อมแจ้งให้เปลี่ยนชื่อแทนการเปลี่ยนชื่อให้เอง

เปิด PowerShell ที่โฟลเดอร์หลักของโปรเจกต์ แล้วสร้าง environment แยก:

```powershell
python -m venv test_endpoint/.venv
test_endpoint/.venv/Scripts/python.exe -m pip install -r test_endpoint/requirements-mock.txt
```

มี `paho-mqtt` ใน dependencies เพราะ client เดิม import ไลบรารีนี้ตั้งแต่เริ่ม
แม้ระบุ `--skip-mqtt` ก็ตาม Tests ใช้ unittest ที่มากับ Python ไม่ต้องติดตั้ง pytest
รองรับ Python 3.10 ขึ้นไป

## 2. เปิดเซิร์ฟเวอร์

แทนที่ลิงก์ตัวอย่างด้วยโฟลเดอร์ภาพของคุณ:

```powershell
test_endpoint/.venv/Scripts/python.exe test_endpoint/mock_server.py --drive-folder "https://drive.google.com/drive/folders/YOUR_FOLDER_ID" --token local-test-token --team-id TEAM_33
```

โปรแกรมจะดาวน์โหลดและตรวจภาพทั้งหมดก่อนเปิด `http://127.0.0.1:8000`
เรียกใหม่ด้วยลิงก์เดิมจะใช้ cache เดิมโดยไม่ติดต่อ Drive จนกว่าจะเพิ่ม `--refresh`:

```powershell
test_endpoint/.venv/Scripts/python.exe test_endpoint/mock_server.py --drive-folder "https://drive.google.com/drive/folders/YOUR_FOLDER_ID" --refresh
```

เปลี่ยนพอร์ตด้วย `--port 8001` และใช้พอร์ตเดียวกันใน client
กด Ctrl+C เพื่อหยุด ระบบรับการเชื่อมต่อจากเครื่องนี้เท่านั้น
ใช้ token จำลองแยกจากรหัสผ่านและ MQTT key จริง ค่าเริ่มต้นคือ `local-test-token`

Cache แยกตาม Drive folder ID อยู่ใน `cache/` ข้างสคริปต์ ใช้ manifest และ SHA-256
ตรวจความครบถ้วนทุกครั้งก่อนใช้ ถ้าดาวน์โหลดไม่ครบ ภาพเสีย ไม่มีภาพ หรือชื่อซ้ำ
โปรแกรมจะไม่เปิด API และไม่ใช้ cache ที่ดาวน์โหลดค้างไว้
`--refresh` สร้าง snapshot ใหม่ และคง snapshot เดิมไว้หากการดาวน์โหลดล้มเหลว
ไฟล์ที่ไม่ใช่ภาพจะข้ามไป การเปลี่ยนไฟล์บน Drive ต้องสั่ง refresh จึงจะเห็นการเปลี่ยนแปลง

ตัวดาวน์โหลด gdown 5.2.2 มีข้อจำกัดการแสดงรายการ 50 items ต่อโฟลเดอร์
โปรแกรมไม่ยอมรับรายการที่ถูกตัดทอน ให้แบ่งเป็นโฟลเดอร์ย่อยขนาดเล็กแทน
ดูรายละเอียด [gdown](https://github.com/wkentaro/gdown/tree/v5.2.2)
หาก Google ปฏิเสธการดาวน์โหลดหรือจำกัดจำนวนครั้ง ต้องแก้สิทธิ์/รอแล้วลองใหม่
ไม่มี OAuth หรือการใช้ cookies ของบัญชีส่วนตัว

## 3. รัน client เดิม

เปิด PowerShell อีก terminal ที่โฟลเดอร์หลัก แล้วรัน:

```powershell
$env:PYTHONIOENCODING = "utf-8"
Push-Location test_endpoint
try {
    ./.venv/Scripts/python.exe ../client.py --http-url http://127.0.0.1:8000 --http-token local-test-token --skip-mqtt
} finally {
    Pop-Location
}
```

การเปลี่ยน working directory นี้ทำให้ภาพที่ client ดาวน์โหลดไปอยู่ใน
`test_endpoint/downloaded_images/` เช่นเดียวกับข้อมูลทดสอบอื่น
หากใช้ Python ที่ติดตั้ง dependencies ครบแล้ว สามารถใช้ `python` แทน Python ใน `.venv` ได้

สิ่งที่ควรเห็น: ดึงภาพสำเร็จ → inference จำลอง → `HTTP Submission ACCEPTED`
→ `MQTT sending skipped` → สถิติ Requested/Submitted เพิ่มอย่างละ 1
ผลที่ server รับจะอยู่ใน `submissions/img_<ID>.json` มีทั้ง `request` และ `response`
แต่ละ GET ออก ID ใหม่ แม้จะวนกลับมาที่ภาพเดิม ไฟล์ภาพฝั่ง client อาจถูกเขียนทับตามชื่อเดิม

## 4. ควบคุมโหมด โควตา และสถานะ

ทุก endpoint ด้านล่างต้องใช้ token เดียวกับ server รวม endpoint ควบคุม mock
ตั้งค่าตัวแปรใน PowerShell:

```powershell
$base = "http://127.0.0.1:8000"
$headers = @{ Authorization = "Bearer local-test-token" }
Invoke-RestMethod "$base/api/v1/stats" -Headers $headers | ConvertTo-Json -Depth 8
```

เปลี่ยนโหมดเริ่มต้นเป็น test (client เดิมไม่ได้ส่ง query mode จึงจะใช้ค่านี้):

```powershell
Invoke-RestMethod "$base/mock/config" -Method Patch -Headers $headers -ContentType "application/json" -Body '{"default_mode":"test","quota_limit":5,"timeout_seconds":60}'
```

`PATCH /mock/config` รับเฉพาะฟิลด์ที่ต้องการเปลี่ยน และคืนค่าปัจจุบันทั้งหมด
ส่ง `{}` เพื่อดูค่าโดยไม่แก้ไข ห้ามส่ง `null`

| ฟิลด์ | ค่าเริ่มต้น | ความหมาย |
|---|---|---|
| `default_mode` | `"train"` | โหมดเมื่อ GET ไม่ระบุ `?mode=` |
| `enabled_modes` | `["train","test"]` | โหมดที่เปิดอยู่ ใช้ `[]` เพื่อปิดทั้งหมด |
| `paused` | `false` | หยุด GET ภาพและ POST ผลชั่วคราว |
| `quota_limit` | `5` | โควตา GET ภาพใน test เป็นจำนวนเต็มตั้งแต่ 0 |
| `timeout_seconds` | `60` | เวลาส่งผล มากกว่า 0 วินาที |

ตัวอย่างหยุดระบบและเปิดกลับ:

```powershell
Invoke-RestMethod "$base/mock/config" -Method Patch -Headers $headers -ContentType "application/json" -Body '{"paused":true}'
Invoke-RestMethod "$base/mock/config" -Method Patch -Headers $headers -ContentType "application/json" -Body '{"paused":false}'
```

ปิด train แต่เปิด test:

```powershell
Invoke-RestMethod "$base/mock/config" -Method Patch -Headers $headers -ContentType "application/json" -Body '{"enabled_modes":["test"],"default_mode":"test"}'
```

ล้างรอบและสถิติ โดยเก็บ config, cache และไฟล์ผลเดิมไว้:

```powershell
Invoke-RestMethod "$base/mock/reset" -Method Post -Headers $headers
```

ค่า config เปลี่ยนแล้วมีผลทันที รวมเวลาส่งผลและโหมดของรอบที่ยังไม่ส่ง
การ pause ไม่หยุดนาฬิกาจับเวลา ลด quota ต่ำกว่าจำนวนที่ขอแล้วจะแสดง remaining เป็น 0
ยังส่งผลของภาพที่ขอไว้แล้วได้แม้โควตาหมด ถ้าโหมดเปิดและยังไม่หมดเวลา

## 5. API และข้อกำหนดของ mock

| Endpoint | ผลลัพธ์ |
|---|---|
| `GET /api/v1/image[?mode=train\|test]` | ไบนารี JPEG/PNG, headers `X-Image-ID`, `X-Image-Name`, `X-Mode` |
| `POST /api/v1/submit` | รับ `{ "img_id": "...", "predictions": { "image_name": "...", "objects": [...] } }` |
| `GET /api/v1/stats` | รูปแบบสถิติเดียวกับ API_DOCS.md |
| `PATCH /mock/config` | แก้ config จำลอง |
| `POST /mock/reset` | ล้างรอบและสถิติ |

แต่ละ object ต้องมี `id` เป็นจำนวนเต็มบวกไม่ซ้ำกัน, `class` เป็นหนึ่งใน
`good`, `shell`, `black`, `husk`, `broken`, `parchment`, `immature`
และ `polygon` อย่างน้อย 3 จุด แต่ละจุดเป็น `[x,y]` ตัวเลข finite
`objects: []` ใช้ได้ ไม่ตรวจขอบภาพ พื้นที่ polygon หรือความถูกต้องเทียบ ground truth
ชื่อภาพต้องตรงกับ header ทุกตัวอักษร ไม่มี confidence/ฟิลด์เสริมใน payload นี้

| HTTP | วิธีทดสอบ |
|---|---|
| `200` | token ถูก ดึงภาพและส่งผลทันเวลา |
| `401` | ใช้ token ผิดหรือไม่ส่ง Bearer token |
| `403` | GET test เกินโควตา หรือ GET/submit ในโหมดที่ปิด |
| `408` | ส่งผลหลัง `timeout_seconds` |
| `409` | ส่งผลที่สำเร็จแล้วซ้ำด้วย `img_id` เดิม |
| `503` | ตั้ง `paused=true` แล้ว GET ภาพหรือ submit |
| `400` | ID ไม่รู้จัก/ส่งก่อนภาพครบ/ชื่อภาพไม่ตรง (ข้อกำหนดเฉพาะ mock) |
| `422` | JSON หรือ query/config ผิดรูปแบบ (ข้อกำหนดเฉพาะ mock) |

นับโควตาที่ GET ภาพสำเร็จ ไม่ใช่ตอนส่งผล การส่งผลที่ถูกปฏิเสธไม่เพิ่มยอด submitted
จับเวลาหลัง ASGI ส่ง body ภาพครบจนถึง handler รับ submission จึงเป็นค่าประมาณ
ไม่ใช่การยืนยันว่าฝั่ง client ได้รับไบต์สุดท้ายแล้ว ช่วงดาวน์โหลดจาก Drive ไม่ถูกนำมาคิด
สถิติและ IDs อยู่ในหน่วยความจำ เริ่มใหม่เมื่อ restart/reset; ไฟล์ submission เดิมยังอยู่
และไม่ถูกนำมาเพิ่มสถิติใหม่ การ reset จะเริ่มจ่ายภาพจากภาพแรกอีกครั้ง

ไฟล์ `client.py` ยังใช้ inference จำลองเดิม ผล counts กับ polygons อาจไม่สอดคล้องกัน
mock ตรวจเฉพาะสัญญา HTTP ไม่ได้ตรวจ counts ที่จะส่ง MQTT และไม่ให้คะแนนโมเดล

## 6. ทดสอบอัตโนมัติ

จากโฟลเดอร์หลัก:

```powershell
test_endpoint/.venv/Scripts/python.exe -m unittest discover -s test_endpoint/tests -v
```

Tests สร้างภาพและไฟล์ชั่วคราวใต้ `test_endpoint/` และลบทิ้งเมื่อจบ
ครอบคลุม headers/binary/วนภาพ, token, ส่งผล/บันทึก JSON, สถิติ, ทุกสถานะผิดพลาด,
โควตาขณะมีคำขอพร้อมกัน, pause/resume/reset, cache/refresh,
การดาวน์โหลดไม่ครบ/ภาพเสีย/ชื่อซ้ำ และการแยกข้อมูลตามโฟลเดอร์ Drive
การทดสอบ cache จำลอง gdown จึงไม่ติดต่อ Google
Smoke test เปิด HTTP บน localhost พอร์ตว่าง รัน client เดิมจริงพร้อม `--skip-mqtt`
แล้วหยุด server ตรวจด้วยว่า client ไม่ถูกแก้และไม่ส่ง MQTT

ทดสอบ Drive จริงโดยใช้ลิงก์ของคุณในขั้นตอน 2 แล้วทำขั้นตอน 3
ต้องมีอินเทอร์เน็ตเฉพาะการดาวน์โหลด/refresh; เมื่อมี cache ครบแล้วทดสอบ HTTP แบบ offline ได้
