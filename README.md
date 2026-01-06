
## FLASK API DICOM ROUTER GATEWAY

### 1. ORTHANC SERVER PACS, DCMTK


```
sudo apt update && sudo apt upgrade -y
sudo apt install dcmtk -y
which dcmdump
dcmdump --version


dcmodify -i "(0010,0020)=P02371443444"          -i "(0008,0050)=202512300001"          0003.DCM

dcmdump 0003.DCM | grep -E "PatientID|AccessionNumber"

docker logs -f dicom-router

sudo apt install orthanc orthanc-dicomweb orthanc-webviewer -y
ls /usr/share/orthanc/plugins/
sudo rm /etc/orthanc/plugins.json
sudo rm /etc/orthanc/webviewer.json
sudo rm /etc/orthanc/serve-folders.json

Melihat Study
curl -u orthanc:orthanc http://192.168.30.21:8042/studies
Uploud File
curl -u orthanc:orthanc -X POST  http://192.168.30.21:8042/instances  -H "Content-Type: application/dicom" --data-binary @0003.DCM
Kirim ke dicomrouter
curl -u orthanc:orthanc -X POST   http://192.168.30.21:8042/modalities/DCMROUTER/store   -H "Content-Type: application/json"   -d '["a5d9d99e-9e18c97b-952af9c9-9a1a3cdc-dabf01a0"]'

sudo nano /etc/orthanc/orthanc.json
```
```json
"DicomModalities" : {
   "DCMROUTER" : [ "DCMROUTER", "192.10.10.51", 11112 ]
}
```
```
sudo systemctl restart orthanc
sudo systemctl status orthanc


mkdir flask-dicom
cd flask-dicom
sudo apt install python3-venv -y
python3 -m venv venv
source venv/bin/activate
pip install flask flask-restx requests
```


### 2. Spesifikasi API Gateway DICOM 

A. POST /api/dicom/raw-send

Endpoint ini digunakan untuk memproses file DICOM yang belum ada di server (masih di komputer lokal user).
- Deskripsi: Upload file ke gateway $\rightarrow$ Modifikasi Tag $\rightarrow$ Upload ke Orthanc $\rightarrow$ Kirim ke Router.
- Content-Type: multipart/form-dataParameter (Form Data):

| Parameter | Tipe | Wajib | Deskripsi |
| :--- | :--- | :--- | :--- |
| file | File | Ya | File biner DICOM (.dcm) |
| patientid | String | Ya | ID Pasien baru yang akan diinject |
| accesionnum | String | Ya | Accession Number baru yang akan diinject |

Respon Sukses (200 OK):
```JSON
{
  "status": "success",
  "orthanc_id": "4a0b467b-2f26e3a1-ed9ee50a-42cad11b-f8f2f90c"
}
```
B. POST /api/dicom/ort-send

Endpoint ini digunakan untuk mengambil data yang sudah ada di Orthanc, mengubah identitasnya, lalu mengirimkannya ulang.
- Deskripsi: Download dari Orthanc $\rightarrow$ Modifikasi Tag $\rightarrow$ Upload kembali $\rightarrow$ Kirim ke Router.
- Content-Type: application/json
- Body Request:
```JSON
{
  "instance_id": "4a0b467b-2f26e3a1-ed9ee50a-42cad11b-f8f2f90c",
  "patientid": "P02371443444",
  "accesionnum": "202512300001"
}
```
- Logika Internal: Gateway menggunakan dcmodify secara sementara sebelum mengunggah kembali instance yang sudah diubah ke database Orthanc.

C. POST /api/dicom/dcm-send
- Endpoint paling sederhana, hanya digunakan untuk memicu pengiriman data (routing) tanpa melakukan perubahan metadata.
- Deskripsi: Instruksi kepada Orthanc untuk mengirim Study ID tertentu ke DICOM Router (AET: DCMROUTER).
- Content-Type: application/json
- Body Request:
```JSON

{
  "study_id": "a5d9d99e-9e18c97b-952af9c9-9a1a3cdc-dabf01a0"
}
```
- Respon Sukses (200 OK):
```JSON
{
  "status": "sent to router"
}
```

```
/project-root
├── app.py
├── templates/
│   └── dcmpage.html
└── temp_dicom/  (akan dibuat otomatis)
```
================

```py
#app.py
import os
import logging
import subprocess
import requests
from flask import Flask
from flask_restx import Api, Resource, Namespace, fields
from werkzeug.datastructures import FileStorage
from logging.handlers import RotatingFileHandler
from flask import render_template

app = Flask(__name__)

# --- CONFIGURATION ---
ORTHANC_URL = "http://192.168.30.21:8042"
ORTHANC_AUTH = ('orthanc', 'orthanc')
MODALITY_NAME = "DCMROUTER"
TEMP_DIR = "temp_dicom"

if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)

# --- LOGGER ---
log_handler = RotatingFileHandler('gateway.log', maxBytes=1000000, backupCount=3)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_handler.setFormatter(formatter)
logger = logging.getLogger('DicomGateway')
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

# --- API SETUP ---
api = Api(app, version='1.0', title='DICOM Gateway API', description='Gateway for Orthanc & DICOM Router')
dicom_ns = Namespace('dicom', description='DICOM Operations')
api.add_namespace(dicom_ns)

# --- MODELS & PARSERS ---
# Parser untuk upload file (A)
raw_upload_parser = dicom_ns.parser()
raw_upload_parser.add_argument('file', location='files', type=FileStorage, required=True)
raw_upload_parser.add_argument('patientid', location='form', type=str, required=True)
raw_upload_parser.add_argument('accesionnum', location='form', type=str, required=True)

# Model untuk edit dari Orthanc (B)
ort_modify_model = dicom_ns.model('OrtModify', {
    'instance_id': fields.String(required=True, description='Orthanc Instance ID'),
    'patientid': fields.String(required=True),
    'accesionnum': fields.String(required=True)
})

# Model untuk kirim langsung (C)
direct_send_model = dicom_ns.model('DirectSend', {
    'study_id': fields.String(required=True, description='Orthanc Study ID')
})

# --- HELPER FUNCTIONS ---
def modify_dicom_tags(filepath, patient_id, accession_num):
    try:
        cmd = [
            "dcmodify", "-i", f"(0010,0020)={patient_id}",
            "-i", f"(0008,0050)={accession_num}", filepath
        ]
        subprocess.run(cmd, check=True)
        return True
    except Exception as e:
        logger.error(f"Error dcmodify: {str(e)}")
        return False

def upload_to_orthanc(filepath):
    with open(filepath, 'rb') as f:
        r = requests.post(f"{ORTHANC_URL}/instances", auth=ORTHANC_AUTH, data=f.read())
        return r.json() if r.status_code == 200 else None

def send_to_router(resource_id):
    # Resource ID bisa berupa Study ID atau Instance ID
    url = f"{ORTHANC_URL}/modalities/{MODALITY_NAME}/store"
    r = requests.post(url, auth=ORTHANC_AUTH, json=[resource_id])
    return r.status_code == 200

# --- ENDPOINTS ---

@dicom_ns.route('/raw-send')
class RawSend(Resource):
    @dicom_ns.expect(raw_upload_parser)
    def post(self):
        """A. Upload Raw -> Modify -> Orthanc -> Router"""
        args = raw_upload_parser.parse_args()
        file = args['file']
        path = os.path.join(TEMP_DIR, file.filename)
        file.save(path)

        # 1 & 2. Modify
        if modify_dicom_tags(path, args['patientid'], args['accesionnum']):
            # 3. Upload Orthanc
            ort_res = upload_to_orthanc(path)
            if ort_res:
                # 4. Send to Router
                success = send_to_router(ort_res['ParentStudy'])
                os.remove(path)
                return {"status": "success", "orthanc_id": ort_res['ID']}, 200
        
        return {"status": "failed"}, 500

@dicom_ns.route('/ort-send')
class OrtSend(Resource):
    @dicom_ns.expect(ort_modify_model)
    def post(self):
        """B. From Orthanc -> Download -> Modify -> Upload Back -> Router"""
        data = dicom_ns.payload
        inst_id = data['instance_id']
        path = os.path.join(TEMP_DIR, f"{inst_id}.dcm")

        # 1. Ambil dari Orthanc
        r = requests.get(f"{ORTHANC_URL}/instances/{inst_id}/file", auth=ORTHANC_AUTH)
        if r.status_code == 200:
            with open(path, 'wb') as f:
                f.write(r.content)
            
            # 2. Modify
            modify_dicom_tags(path, data['patientid'], data['accesionnum'])
            
            # 3. Upload back
            ort_res = upload_to_orthanc(path)
            
            # 4. Send to router
            send_to_router(ort_res['ParentStudy'])
            os.remove(path)
            return {"status": "success", "new_id": ort_res['ID']}, 200
        
        return {"status": "failed"}, 404

@dicom_ns.route('/dcm-send')
class DcmSend(Resource):
    @dicom_ns.expect(direct_send_model)
    def post(self):
        """C. Direct Send from Orthanc to Router"""
        data = dicom_ns.payload
        if send_to_router(data['study_id']):
            return {"status": "sent to router"}, 200
        return {"status": "failed"}, 500

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)

```


```py
import os
import logging
import subprocess
import requests
from flask import Flask, render_template, jsonify
from flask_restx import Api, Resource, Namespace, fields
from werkzeug.datastructures import FileStorage
from logging.handlers import RotatingFileHandler

app = Flask(__name__)

# --- CONFIGURATION ---
ORTHANC_URL = "http://192.168.30.21:8042"
ORTHANC_AUTH = ('orthanc', 'orthanc')
MODALITY_NAME = "DCMROUTER"
TEMP_DIR = "temp_dicom"
LOG_FILE = "app_dicom.log"

if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)

# --- LOGGER CONFIGURATION ---
log_handler = RotatingFileHandler(LOG_FILE, maxBytes=500000, backupCount=2)
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_handler.setFormatter(log_formatter)

logger = logging.getLogger('DicomGateway')
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

# --- FLASK-RESTX SETUP ---
api = Api(app, version='1.0', title='DICOM Gateway API', 
          description='Gateway untuk Manipulasi dan Pengiriman DICOM', 
          doc='/api/docs', prefix='/api')

dicom_ns = Namespace('dicom', description='Operasi DICOM ke Router')
api.add_namespace(dicom_ns)

# --- MODELS & PARSERS ---
# A. Parser untuk Raw Upload
raw_upload_parser = dicom_ns.parser()
raw_upload_parser.add_argument('file', location='files', type=FileStorage, required=True, help='File DICOM')
raw_upload_parser.add_argument('patientid', location='form', type=str, required=True)
raw_upload_parser.add_argument('accesionnum', location='form', type=str, required=True)

# B. Model untuk Edit dari Orthanc
ort_modify_model = dicom_ns.model('OrtModify', {
    'instance_id': fields.String(required=True, example='4a0b467b-2f26e3a1...'),
    'patientid': fields.String(required=True, example='P12345'),
    'accesionnum': fields.String(required=True, example='ACC999')
})

# C. Model untuk Direct Send
direct_send_model = dicom_ns.model('DirectSend', {
    'study_id': fields.String(required=True, example='a5d9d99e-9e18c97b...')
})

# --- HELPER FUNCTIONS ---
def modify_dicom_tags(filepath, patient_id, accession_num):
    try:
        logger.info(f"Memulai modifikasi file: {filepath}")
        cmd = [
            "dcmodify", "-i", f"(0010,0020)={patient_id}",
            "-i", f"(0008,0050)={accession_num}", filepath
        ]
        subprocess.run(cmd, check=True)
        return True
    except Exception as e:
        logger.error(f"Gagal modifikasi DCM: {str(e)}")
        return False

def upload_to_orthanc(filepath):
    try:
        with open(filepath, 'rb') as f:
            r = requests.post(f"{ORTHANC_URL}/instances", auth=ORTHANC_AUTH, data=f.read())
            if r.status_code == 200:
                logger.info(f"Berhasil upload ke Orthanc: {r.json()['ID']}")
                return r.json()
        return None
    except Exception as e:
        logger.error(f"Gagal upload ke Orthanc: {str(e)}")
        return None

def send_to_router(study_id):
    try:
        url = f"{ORTHANC_URL}/modalities/{MODALITY_NAME}/store"
        r = requests.post(url, auth=ORTHANC_AUTH, json=[study_id])
        if r.status_code == 200:
            logger.info(f"Berhasil instruksi kirim ke {MODALITY_NAME} untuk study {study_id}")
            return True
        return False
    except Exception as e:
        logger.error(f"Gagal mengirim ke router: {str(e)}")
        return False

# --- WEB UI ROUTES ---
@app.route("/")
def index():
    return render_template("dcmpage.html")

@app.route("/api/logs")
def get_logs():
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                lines = f.readlines()
                return "".join(lines[-30:])
        return "Log file not found."
    except Exception as e:
        return str(e)

# --- API ENDPOINTS ---

@dicom_ns.route('/raw-send')
class RawSend(Resource):
    @dicom_ns.expect(raw_upload_parser)
    def post(self):
        """A. Upload Raw -> Modify -> Orthanc -> Router"""
        args = raw_upload_parser.parse_args()
        file = args['file']
        path = os.path.join(TEMP_DIR, f"raw_{file.filename}")
        file.save(path)

        logger.info(f"Menerima upload manual: {file.filename}")
        
        if modify_dicom_tags(path, args['patientid'], args['accesionnum']):
            ort_res = upload_to_orthanc(path)
            if ort_res:
                send_to_router(ort_res['ParentStudy'])
                if os.path.exists(path): os.remove(path)
                return {"status": "success", "id": ort_res['ID']}, 200
        
        return {"status": "error", "message": "Proses gagal"}, 500

@dicom_ns.route('/ort-send')
class OrtSend(Resource):
    @dicom_ns.expect(ort_modify_model)
    def post(self):
        """B. Ambil dari Orthanc -> Modify -> Re-upload -> Router"""
        data = dicom_ns.payload
        inst_id = data['instance_id']
        path = os.path.join(TEMP_DIR, f"mod_{inst_id}.dcm")

        logger.info(f"Memproses modifikasi dari Orthanc ID: {inst_id}")

        r = requests.get(f"{ORTHANC_URL}/instances/{inst_id}/file", auth=ORTHANC_AUTH)
        if r.status_code == 200:
            with open(path, 'wb') as f:
                f.write(r.content)
            
            if modify_dicom_tags(path, data['patientid'], data['accesionnum']):
                ort_res = upload_to_orthanc(path)
                send_to_router(ort_res['ParentStudy'])
                if os.path.exists(path): os.remove(path)
                return {"status": "success", "new_id": ort_res['ID']}, 200
        
        return {"status": "error", "message": "Gagal mengambil/memproses data"}, 500

@dicom_ns.route('/dcm-send')
class DcmSend(Resource):
    @dicom_ns.expect(direct_send_model)
    def post(self):
        """C. Kirim langsung Study dari Orthanc ke Router"""
        data = dicom_ns.payload
        logger.info(f"Direct send dipicu untuk Study: {data['study_id']}")
        if send_to_router(data['study_id']):
            return {"status": "success", "message": "Terkirim ke router"}, 200
        return {"status": "error", "message": "Gagal mengirim"}, 500

if __name__ == '__main__':
    # Jalankan server
    app.run(host='0.0.0.0', port=5000, debug=True)
```
templates/dcmpage.html
```html
<!DOCTYPE html>
<html lang="id">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>DICOM Gateway Dashboard</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
    <style>
        :root { --bg-color: #f4f7f6; --console-bg: #1e1e1e; --console-text: #00ff00; }
        body { background-color: var(--bg-color); font-family: 'Segoe UI', sans-serif; }
        .card { border-radius: 12px; border: none; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        .log-container {
            background: var(--console-bg); color: var(--console-text); padding: 15px;
            height: 480px; overflow-y: auto; font-family: monospace; font-size: 12px;
            border-radius: 8px; border: 2px solid #333; white-space: pre-wrap;
        }
        .nav-tabs .nav-link.active { color: #0d6efd; border-bottom: 3px solid #0d6efd; background: transparent; }
    </style>
</head>
<body>

<nav class="navbar navbar-expand-lg navbar-dark bg-dark mb-4 shadow">
    <div class="container">
        <a class="navbar-brand" href="#"><i class="fa-solid fa-microchip me-2 text-info"></i>DICOM Gateway <span class="badge bg-secondary ms-2">RPi v1.0</span></a>
        <div class="navbar-nav ms-auto">
            <a class="nav-link" href="/api/docs" target="_blank"><i class="fa-solid fa-book me-1"></i> API Docs</a>
        </div>
    </div>
</nav>

<div class="container">
    <div class="row g-4">
        <div class="col-lg-6">
            <div class="card shadow-sm">
                <div class="card-header py-3">
                    <ul class="nav nav-tabs card-header-tabs" id="dicomTab" role="tablist">
                        <li class="nav-item"><button class="nav-link active" data-bs-toggle="tab" data-bs-target="#tab-upload">Upload Manual</button></li>
                        <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-fetch">Orthanc Modify</button></li>
                        <li class="nav-item"><button class="nav-link" data-bs-toggle="tab" data-bs-target="#tab-direct">Direct Send</button></li>
                    </ul>
                </div>
                <div class="card-body tab-content">
                    <div class="tab-pane fade show active" id="tab-upload">
                        <form id="formRaw">
                            <div class="mb-3">
                                <label class="form-label">Pilih File DICOM</label>
                                <input type="file" class="form-control" name="file" required>
                            </div>
                            <div class="row">
                                <div class="col"><input type="text" class="form-control" name="patientid" placeholder="New Patient ID" required></div>
                                <div class="col"><input type="text" class="form-control" name="accesionnum" placeholder="New Accession" required></div>
                            </div>
                            <button type="submit" class="btn btn-primary mt-3 w-100">Upload & Send</button>
                        </form>
                    </div>

                    <div class="tab-pane fade" id="tab-fetch">
                        <form id="formOrt">
                            <div class="mb-3">
                                <label class="form-label">Instance ID Orthanc</label>
                                <input type="text" class="form-control" name="instance_id" placeholder="e.g. 4a0b467b-..." required>
                            </div>
                            <div class="row">
                                <div class="col"><input type="text" class="form-control" name="patientid" placeholder="New Patient ID" required></div>
                                <div class="col"><input type="text" class="form-control" name="accesionnum" placeholder="New Accession" required></div>
                            </div>
                            <button type="submit" class="btn btn-success mt-3 w-100">Fetch, Modify & Send</button>
                        </form>
                    </div>

                    <div class="tab-pane fade" id="tab-direct">
                        <form id="formDirect">
                            <div class="mb-3">
                                <label class="form-label">Study ID Orthanc</label>
                                <input type="text" class="form-control" name="study_id" placeholder="ID Study untuk dikirim langsung" required>
                            </div>
                            <button type="submit" class="btn btn-warning mt-3 w-100">Direct Send to Router</button>
                        </form>
                    </div>
                </div>
            </div>
        </div>

        <div class="col-lg-6">
            <div class="card shadow-sm">
                <div class="card-header py-3 d-flex justify-content-between align-items-center">
                    <span><i class="fa-solid fa-terminal1e1e1e; --console-text: #00ff00; }
        body { background-color: var(--bg-color); font-family: 'Segoe UI', sans-serif; }
        .card { border-radius: 12px; border: none; box-shadow: 0 4px 6px rgba(0,0,0,0.1); }
        .log-container {
            background: var(--console-bg); color: var(--console-text); padding: 15px;
            height: 480px; overflow-y: auto; font-family: monospace; font-size: 12px;
            border-radius: 8px; border: 2px solid #333; white-space: pre-wrap;
        }
        .nav-tabs .nav-link.active { color: #0d6efd; border-bottom: 3px solid #0d6efd; background: transparent; }
    </style>
</head>
<body> me-2"></i>Live System Log</span>
                    <button class="btn btn-sm btn-outline-danger" onclick="document.getElementById('logConsole').innerHTML='' ">Clear</button>
                </div>
                <div class="card-body">
                    <div id="logConsole" class="log-container">Menunggu data...</div>
                </div>
            </div>
        </div>
    </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
<script>
    // Fungsi untuk Update Log Otomatis
    setInterval(() => {
        fetch('/api/logs').then(r => r.text()).then(data => {
            const el = document.getElementById('logConsole');
            el.innerText = data;
            el.scrollTop = el.scrollHeight;
        });
    }, 2000);

    // Logic Submit Form A (Raw Send)
    document.getElementById('formRaw').onsubmit = async (e) => {
        e.preventDefault();
        const formData = new FormData(e.target);
        const res = await fetch('/api/dicom/raw-send', { method: 'POST', body: formData });
        alert(res.ok ? "Berhasil dikirim!" : "Gagal!");
    };

    // Logic Submit Form B (Ort Send)
    document.getElementById('formOrt').onsubmit = async (e) => {
        e.preventDefault();
        const data = Object.fromEntries(new FormData(e.target));
        const res = await fetch('/api/dicom/ort-send', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        alert(res.ok ? "Berhasil dimodifikasi & kirim!" : "Gagal!");
    };

    // Logic Submit Form C (Direct Send)
    document.getElementById('formDirect').onsubmit = async (e) => {
        e.preventDefault();
        const data = Object.fromEntries(new FormData(e.target));
        const res = await fetch('/api/dicom/dcm-send', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(data)
        });
        alert(res.ok ? "Perintah kirim berhasil!" : "Gagal!");
    };
</script>
</body>
</html>
```


========================================================



sudo apt update && sudo apt upgrade -y
sudo apt install dcmtk -y
which dcmdump
dcmdump --version


dcmodify -i "(0010,0020)=P02371443444"          -i "(0008,0050)=202512300001"          0003.DCM

dcmdump 0003.DCM | grep -E "PatientID|AccessionNumber"

docker logs -f dicom-router

sudo apt install orthanc orthanc-dicomweb orthanc-webviewer -y
ls /usr/share/orthanc/plugins/
sudo rm /etc/orthanc/plugins.json
sudo rm /etc/orthanc/webviewer.json
sudo rm /etc/orthanc/serve-folders.json

Melihat Study
curl -u orthanc:orthanc http://192.168.30.21:8042/studies
Uploud File
curl -u orthanc:orthanc -X POST  http://192.168.30.21:8042/instances  -H "Content-Type: application/dicom" --data-binary @0003.DCM
Kirim ke dicomrouter
curl -u orthanc:orthanc -X POST   http://192.168.30.21:8042/modalities/DCMROUTER/store   -H "Content-Type: application/json"   -d '["a5d9d99e-9e18c97b-952af9c9-9a1a3cdc-dabf01a0"]'

sudo nano /etc/orthanc/orthanc.json
sudo systemctl restart orthanc
sudo systemctl status orthanc


mkdir flask-dicom
cd flask-dicom
sudo apt install python3-venv -y
python3 -m venv venv
source venv/bin/activate
pip install pydicom flask


Spesifikasi API Gateway DICOM
A. POST /api/dicom/raw-sendEndpoint ini digunakan untuk memproses file DICOM yang belum ada di server (masih di komputer lokal user).
Deskripsi: Upload file ke gateway $\rightarrow$ Modifikasi Tag $\rightarrow$ Upload ke Orthanc $\rightarrow$ Kirim ke Router.
Content-Type: multipart/form-data
Parameter (Form Data):| Parameter | Tipe | Wajib | Deskripsi || :--- | :--- | :--- | :--- || file | File | Ya | File biner DICOM (.dcm) || patientid | String | Ya | ID Pasien baru yang akan disuntikkan || accesionnum | String | Ya | Accession Number baru yang akan disuntikkan |Respon Sukses (200 OK):JSON{
  "status": "success",
  "orthanc_id": "4a0b467b-2f26e3a1-ed9ee50a-42cad11b-f8f2f90c"
}
B. POST /api/dicom/ort-sendEndpoint ini digunakan untuk mengambil data yang sudah ada di Orthanc, mengubah identitasnya, lalu mengirimkannya ulang.Deskripsi: Download dari Orthanc $\rightarrow$ Modifikasi Tag $\rightarrow$ Upload kembali $\rightarrow$ Kirim ke Router.Content-Type: application/jsonBody Request:JSON{
  "instance_id": "4a0b467b-2f26e3a1-ed9ee50a-42cad11b-f8f2f90c",
  "patientid": "P02371443444",
  "accesionnum": "202512300001"
}
Logika Internal: Gateway menggunakan dcmodify secara sementara sebelum mengunggah kembali instance yang sudah diubah ke database Orthanc.C. POST /api/dicom/dcm-sendEndpoint paling sederhana, hanya digunakan untuk memicu pengiriman data (routing) tanpa melakukan perubahan metadata.Deskripsi: Instruksi kepada Orthanc untuk mengirim Study ID tertentu ke DICOM Router (AET: DCMROUTER).Content-Type: application/jsonBody Request:JSON{
  "study_id": "a5d9d99e-9e18c97b-952af9c9-9a1a3cdc-dabf01a0"
}
Respon Sukses (200 OK):JSON{
  "status": "sent to router"
}


perbaiki tahapan api ini /ort-send

1. Cari StudyInstanceUID

http://192.168.30.14:5000/api/dicom/find
curl -X 'POST' \
  'http://192.168.30.14:5000/api/dicom/find' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
  "StudyInstanceUID": "1.3.46.670589.30.39.0.1.966169802732.1695722393202.2"
}'
{
  "status": "error",
  "message": "Study Instance UID tidak ditemukan di database Orthanc"
}

jika tidak ketemu >> response peringatan dicom file tidak ditemukan dan logger

curl -X 'POST' \
  'http://192.168.30.14:5000/api/dicom/find' \
  -H 'accept: application/json' \
  -H 'Content-Type: application/json' \
  -d '{
  "StudyInstanceUID": "1.3.46.670589.30.39.0.1.966169802732.1695722393202.1"
}'

{
  "status": "success",
  "orthanc_study_id": "09cfea59-7f983b2c-9330afba-0b615dc4-71852d27",
  "total_instances": 1,
  "instance_ids": [
    "6b0b9e71-6410ccf0-711961bc-9533b003-e3041b0d"
  ],
  "sample_instance_id": "6b0b9e71-6410ccf0-711961bc-9533b003-e3041b0d"
}

jika ketemu >> logger "sample_instance_id": "6b0b9e71-6410ccf0-711961bc-9533b003-e3041b0d" >> 2 

2. download file dicom ke lokal folder

curl -u orthanc:orthanc http://192.168.30.21:8042/instances/6b0b9e71-6410ccf0-711961bc-9533b003-e3041b0d/file > /tmp/test_file.dcm

curl -u orthanc:orthanc http://192.168.30.21:8042/instances/6b0b9e71-6410ccf0-711961bc-9533b003-e3041b0d/file > coba_file.dcm
  % Total    % Received % Xferd  Average Speed   Time    Time     Time  Current
                                 Dload  Upload   Total   Spent    Left  Speed
100 6637k  100 6637k    0     0  75.5M      0 --:--:-- --:--:-- --:--:-- 76.2M
logger "file selesai download" >> 3 

3. modify dicom tag
modify_dicom_tags(path, args['patientid'], args['accesionnum'])
logger "file selesai modify" >> 4
 
4. kirim ke dicom router
send_to_router(ort_res['ParentStudy'])
logger "file sukses dikirim dicom router"


========