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
        data = dicom_ns.payload
        
        # Ambil dan bersihkan data dari spasi/newline
        study_iuid = str(data.get('study_iuid', '')).strip()
        patient_id = str(data.get('patientid', '')).strip()
        accession_num = str(data.get('accesionnum', '')).strip()

        # Validasi awal agar tidak mengirim payload kosong ke Orthanc
        if not study_iuid:
            return {"status": "error", "message": "StudyInstanceUID (study_iuid) wajib diisi"}, 400

        # --- 1. CARI STUDY UUID ---
        search_payload = {
            "Level": "Study",
            "Query": {
                "StudyInstanceUID": study_iuid
            }
        }
        
        # Debugging: Print ini ke console Anda untuk memastikan sama dengan CURL
        logger.info(f"Target URL: {ORTHANC_URL}/tools/find")
        logger.info(f"Payload dikirim: {search_payload}")

        try:
            search_res = requests.post(
                f"{ORTHANC_URL}/tools/find", 
                json=search_payload, 
                auth=ORTHANC_AUTH,
                headers={"Accept": "application/json"} # Memastikan format response JSON
            )
            
            # Jika error 400, kita log detail pesan error dari Orthanc
            if search_res.status_code != 200:
                logger.error(f"Orthanc Response ({search_res.status_code}): {search_res.text}")
                return {
                    "status": "error", 
                    "message": f"Orthanc Error: {search_res.text}"
                }, search_res.status_code

            studies = search_res.json()

        except requests.exceptions.RequestException as e:
            logger.error(f"Koneksi Orthanc Error: {str(e)}")
            return {"status": "error", "message": "Koneksi ke server Orthanc bermasalah"}, 500
        except Exception as e:
            logger.error(f"Error pada tahapan ort-send: {str(e)}")
            # Cleanup jika terjadi error di tengah jalan
            if 'path' in locals() and os.path.exists(path): os.remove(path)
            return {"status": "error", "message": str(e)}, 500

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

# Tambahkan model input untuk Swagger (jika belum ada)
find_model = dicom_ns.model('FindStudy', {
    'StudyInstanceUID': fields.String(required=True, description='DICOM Study Instance UID')
})

@dicom_ns.route('/find')
class FindDicom(Resource):
    @dicom_ns.expect(find_model)
    def post(self):
        """Cari ID Orthanc berdasarkan StudyInstanceUID"""
        data = dicom_ns.payload
        study_iuid = data.get('StudyInstanceUID')

        # 1. Hit ke endpoint /tools/find milik Orthanc
        search_payload = {
            "Level": "Study",
            "Query": {
                "StudyInstanceUID": study_iuid
            }
        }

        try:
            logger.info(f"Mencari StudyInstanceUID: {study_iuid}")
            
            # Request ke Orthanc
            response = requests.post(
                f"{ORTHANC_URL}/tools/find", 
                json=search_payload, 
                auth=ORTHANC_AUTH
            )
            
            # Orthanc /tools/find mengembalikan LIST of Strings (ID)
            study_ids = response.json()

            if not isinstance(study_ids, list) or len(study_ids) == 0:
                return {
                    "status": "error", 
                    "message": "Study Instance UID tidak ditemukan di database Orthanc"
                }, 404

            # 2. Ambil ID Study pertama yang ditemukan
            orthanc_study_id = study_ids[0]

            # 3. Ambil daftar Instance agar kita bisa dapatkan ID untuk di-download
            inst_response = requests.get(
                f"{ORTHANC_URL}/studies/{orthanc_study_id}/instances", 
                auth=ORTHANC_AUTH
            )
            instances_data = inst_response.json()
            
            # Ambil hanya ID instance-nya saja untuk mempermudah pembacaan
            list_instance_ids = [inst['ID'] for inst in instances_data]

            return {
                "status": "success",
                "orthanc_study_id": orthanc_study_id,
                "total_instances": len(list_instance_ids),
                "instance_ids": list_instance_ids,
                "sample_instance_id": list_instance_ids[0] if list_instance_ids else None
            }, 200

        except Exception as e:
            logger.error(f"Error saat mencari study: {str(e)}")
            return {"status": "error", "message": str(e)}, 500

if __name__ == '__main__':
    # Jalankan server
    app.run(host='0.0.0.0', port=5000, debug=True)