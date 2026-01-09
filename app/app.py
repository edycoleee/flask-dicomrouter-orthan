import os
import logging
import subprocess
import requests
import shutil
import uuid
from flask import Flask, render_template, jsonify
from flask_restx import Api, Resource, Namespace, fields
from werkzeug.datastructures import FileStorage
from logging.handlers import RotatingFileHandler

app = Flask(__name__)

# --- CONFIGURATION ---
ORTHANC_URL = "http://192.168.171.85:8042"
ORTHANC_AUTH = ('orthanc', 'orthanc')
MODALITY_NAME = "DCMROUTER" 
TEMP_BASE_DIR = "temp_dicom"
LOG_FILE = "app_dicom.log"

if not os.path.exists(TEMP_BASE_DIR):
    os.makedirs(TEMP_BASE_DIR)

# --- LOGGER CONFIGURATION ---
log_handler = RotatingFileHandler(LOG_FILE, maxBytes=1000000, backupCount=3)
log_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
log_handler.setFormatter(log_formatter)

logger = logging.getLogger('DicomGateway')
logger.setLevel(logging.INFO)
logger.addHandler(log_handler)

# --- FLASK-RESTX SETUP ---
api = Api(app, version='1.0', title='DICOM Gateway API dengan ORTHANC', 
          description='Gateway untuk Manipulasi dan Pengiriman DICOM ke dicom-router satusehat', 
          doc='/api/docs', prefix='/api')

dicom_ns = Namespace('dicom', description='Operasi DICOM ke Router')
api.add_namespace(dicom_ns)

# --- MODELS & PARSERS ---
raw_upload_parser = dicom_ns.parser()
raw_upload_parser.add_argument('file', location='files', type=FileStorage, required=True)
raw_upload_parser.add_argument('patientid', location='form', type=str, required=True)
raw_upload_parser.add_argument('accesionnum', location='form', type=str, required=True)

modify_model = dicom_ns.model('ModifyModel', {
    'StudyInstanceUID': fields.String(required=True, example='1.3.46...'),
    'patientid': fields.String(required=True, example='P00001349...'),
    'accesionnum': fields.String(required=True, example='202512300002')
})

study_uid_model = dicom_ns.model('StudyUIDModel', {
    'StudyInstanceUID': fields.String(required=True, example='1.3.46...')
})

delete_file_parser = dicom_ns.parser()
delete_file_parser.add_argument('filename', type=str, required=True)

# --- SATUSEHAT CONFIG ---
AUTH_URL = "https://api-satusehat.kemkes.go.id/oauth2/v1"
BASE_URL = "https://api-satusehat.kemkes.go.id/fhir-r4/v1"
ORG_ID = "1000xxxxxx"
CLIENT_ID = "Gzn7Yjxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
CLIENT_SECRET = "fbPy8xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# --- HELPER SATUSEHAT ---
def fetch_ss_token():
    token_url = f"{AUTH_URL}/accesstoken?grant_type=client_credentials"
    try:
        resp = requests.post(token_url, data={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET}, timeout=15)
        resp.raise_for_status()
        return resp.json().get("access_token"), None
    except Exception as e:
        return None, str(e)

def fhir_get(url, token):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/fhir+json"}
    try:
        resp = requests.get(url, headers=headers, timeout=20)
        return resp.json(), resp.status_code
    except Exception as e:
        return {"error": str(e)}, 502

# --- HELPER FUNCTIONS ---
def modify_dicom_tags(filepath, patient_id, accession_num):
    try:
        cmd = ["dcmodify", "-nb", "-i", f"(0010,0020)={patient_id}", "-i", f"(0008,0050)={accession_num}", filepath]
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except Exception as e:
        logger.error(f"dcmodify error: {str(e)}")
        return False

def upload_to_orthanc(filepath):
    try:
        with open(filepath, 'rb') as f:
            r = requests.post(f"{ORTHANC_URL}/instances", auth=ORTHANC_AUTH, data=f.read())
            return r.json() if r.status_code == 200 else None
    except Exception as e:
        logger.error(f"Upload error: {str(e)}")
        return None

def send_to_router(orthanc_study_id):
    try:
        r = requests.post(f"{ORTHANC_URL}/modalities/{MODALITY_NAME}/store", auth=ORTHANC_AUTH, data=orthanc_study_id)
        return r.status_code == 200
    except Exception as e:
        logger.error(f"C-STORE error: {str(e)}")
        return False

def find_study_by_uid(uid):
    payload = {"Level": "Study", "Query": {"StudyInstanceUID": uid}}
    r = requests.post(f"{ORTHANC_URL}/tools/find", json=payload, auth=ORTHANC_AUTH)
    ids = r.json()
    return ids[0] if ids else None

# --- API ENDPOINTS ---

@dicom_ns.route('/raw-send')
class RawSend(Resource):
    @dicom_ns.expect(raw_upload_parser)
    def post(self):
        """Upload file dcm baru -> Modify -> Orthanc -> Router"""
        args = raw_upload_parser.parse_args()
        file = args['file']
        patient_id = args['patientid']
        accession_num = args['accesionnum']

        # 1. Simpan DICOM di lokal dengan nama uuid4.dcm
        request_id = str(uuid.uuid4())
        request_dir = os.path.join(TEMP_BASE_DIR, request_id)
        os.makedirs(request_dir, exist_ok=True)
        
        filename = f"{request_id}.dcm"
        path = os.path.join(request_dir, filename)

        try:
            # Step 1: Upload ke Server API
            file.save(path)
            logger.info(f"Step 1: Upload File ke Server API Berhasil: {filename}")

            # 2. Modify DICOM Tag (PatientID, AccessionNumber)
            if modify_dicom_tags(path, patient_id, accession_num):
                logger.info(f"Step 2: Modify dicom tag Berhasil: {patient_id}, {accession_num}")
                
                # 3. Upload ke Orthanc
                ort_res = upload_to_orthanc(path)
                
                if ort_res and ort_res.get('Status') == 'Success':
                    instance_id = ort_res['ID']
                    study_id = ort_res['ParentStudy'] # Menggunakan Study ID untuk router biasanya lebih umum
                    logger.info(f"Step 3: Upload file ke orthanc Berhasil: {instance_id},{study_id}")

                    # 4. Send dari Orthanc ke DICOM Router
                    # Catatan: send_to_router menggunakan ID internal Orthanc
                    if send_to_router(study_id):
                        logger.info(f"Step 4: Orthanc ke dicom router Berhasil: {study_id}")
                        return {
                            "status": "success", 
                            "message": "File processed and sent to router",
                            "orthanc_instance_id": instance_id
                        }, 200
                    else:
                        logger.error("Step 4 Gagal: Gagal mengirim ke DICOM Router")
                        return {"status": "error", "message": "Gagal kirim ke DICOM Router"}, 500
                else:
                    logger.error("Step 3 Gagal: Gagal upload ke Orthanc")
                    return {"status": "error", "message": "Gagal upload ke Orthanc"}, 500
            else:
                logger.error("Step 2 Gagal: Gagal modifikasi tag DICOM")
                return {"status": "error", "message": "Gagal modifikasi tag DICOM"}, 500

        except Exception as e:
            logger.error(f"General Error: {str(e)}")
            return {"status": "error", "message": str(e)}, 500
        
        finally:
            # Cleanup: Hapus file temporary setelah selesai
            if os.path.exists(request_dir):
                shutil.rmtree(request_dir, ignore_errors=True)
                logger.info(f"Cleanup: Temporary file {filename} dihapus")

@dicom_ns.route('/direct-send')
class DirectSend(Resource):
    @dicom_ns.expect(study_uid_model)
    def post(self):
        """Orthanc -> Router"""
        uid = dicom_ns.payload.get('StudyInstanceUID')
        orthanc_id = find_study_by_uid(uid)
        if not orthanc_id: return {"status": "error", "message": "Study not found"}, 404
        if send_to_router(orthanc_id): return {"status": "success"}, 200
        return {"status": "error", "message": "Send failed"}, 500

@dicom_ns.route('/modify-and-send')
class ModifyAndSend(Resource):
    @dicom_ns.expect(modify_model)
    def post(self):
        """Orthanc -> Modify -> Orthanc, hapus yg lama -> Router"""
        data = dicom_ns.payload
        old_id = find_study_by_uid(data['StudyInstanceUID'])
        if not old_id: return {"status": "error", "message": "Source study not found"}, 404
        
        request_dir = os.path.join(TEMP_BASE_DIR, str(uuid.uuid4()))
        os.makedirs(request_dir)
        try:
            instances = requests.get(f"{ORTHANC_URL}/studies/{old_id}/instances", auth=ORTHANC_AUTH).json()
            new_study_id = None
            for inst in instances:
                f_path = os.path.join(request_dir, f"{inst['ID']}.dcm")
                r_file = requests.get(f"{ORTHANC_URL}/instances/{inst['ID']}/file", auth=ORTHANC_AUTH)
                with open(f_path, 'wb') as f: f.write(r_file.content)
                if modify_dicom_tags(f_path, data['patientid'], data['accesionnum']):
                    up = upload_to_orthanc(f_path)
                    if up: new_study_id = up['ParentStudy']
            if new_study_id:
                send_to_router(new_study_id)
                requests.delete(f"{ORTHANC_URL}/studies/{old_id}", auth=ORTHANC_AUTH)
                return {"status": "success", "new_study_id": new_study_id}, 200
            return {"status": "error", "message": "Modification failed"}, 500
        finally:
            shutil.rmtree(request_dir, ignore_errors=True)

@dicom_ns.route('/temp-files')
class TempFiles(Resource):
    def get(self):
        """Lihat temp file"""
        files = []
        for root, _, filenames in os.walk(TEMP_BASE_DIR):
            for f in filenames:
                fp = os.path.join(root, f)
                files.append({"name": f, "size": f"{round(os.path.getsize(fp)/1024,1)}KB", "rel": os.path.relpath(fp, TEMP_BASE_DIR)})
        return {"files": files}
    
    def delete(self):
        """Hapus semua temp file"""
        shutil.rmtree(TEMP_BASE_DIR)
        os.makedirs(TEMP_BASE_DIR)
        return {"message": "Temp cleared"}

@dicom_ns.route('/temp-file')
class DeleteSingleFile(Resource):
    @dicom_ns.expect(delete_file_parser)
    def delete(self):
        """Hapus temp file by nama file"""
        fname = delete_file_parser.parse_args()['filename']
        for root, _, filenames in os.walk(TEMP_BASE_DIR):
            if fname in filenames:
                os.remove(os.path.join(root, fname))
                return {"message": "Deleted"}
        return {"message": "Not found"}, 404


@dicom_ns.route('/imageid/<string:acsn>')
@dicom_ns.doc(params={'acsn': 'Accession Number dari PACS/SatuSehat'})
class ImageId(Resource):
    def get(self, acsn):
        """Ambil ImagingStudy ID dari SatuSehat berdasarkan Accession Number"""
        token, err = fetch_ss_token()
        if err:
            logger.error(f"Auth SatuSehat failed")
            return {"status": "error", "message": "Auth SatuSehat failed", "detail": err}, 502
        
        identifier_system = f"http://sys-ids.kemkes.go.id/acsn/{ORG_ID}"
        url = f"{BASE_URL}/ImagingStudy?identifier={identifier_system}|{acsn}"
        
        data, status = fhir_get(url, token)
        
        if status != 200:
            return {"status": "error", "detail": data}, status

        # Parsing Bundle response
        if data.get("resourceType") == "Bundle":
            entries = data.get("entry") or []
            for e in entries:
                res = e.get("resource") or {}
                if res.get("resourceType") == "ImagingStudy":
                    logger.info(f"imagingStudy_id :", res.get("id"))    
                    return {
                        "status": "success",
                        "imagingStudy_id": res.get("id"),
                        "patient_reference": res.get("subject", {}).get("reference")
                    }, 200

        logger.error(f"No ImagingStudy found for this Accession Number")    
        return {"status": "error", "message": "No ImagingStudy found for this Accession Number"}, 404

# --- WEB ROUTES ---
@app.route("/")
def index(): return render_template("dcmpage.html")

@app.route("/api/logs")
def get_logs():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as f: return "".join(f.readlines()[-30:])
    return "No logs."

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)