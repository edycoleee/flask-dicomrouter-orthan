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
api = Api(app, version='1.0', title='DICOM Gateway API', 
          description='Gateway untuk Manipulasi dan Pengiriman DICOM', 
          doc='/api/docs', prefix='/api')

dicom_ns = Namespace('dicom', description='Operasi DICOM ke Router')
api.add_namespace(dicom_ns)


# --- HELPER FUNCTIONS ---
find_model = dicom_ns.model('FindStudy', {
    'StudyInstanceUID': fields.String(required=True, description='DICOM Study Instance UID')
})


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

# Find orthancid, parentid dari StudyInstanceUID
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


upload_parser = dicom_ns.parser()
upload_parser.add_argument('file', location='files', type=FileStorage, required=True)


@dicom_ns.route('/upload-orth')
class UploadToOrthanc(Resource):
    @dicom_ns.expect(upload_parser)
    def post(self):
        """Upload file DICOM ke Orthanc dan return orthanc_study_id"""
        args = upload_parser.parse_args()
        file = args['file']

        if file is None:
            return {"status": "error", "message": "File tidak ditemukan"}, 400

        try:
            # 1. Simpan file sementara
            temp_id = str(uuid.uuid4())
            temp_path = os.path.join(TEMP_BASE_DIR, f"{temp_id}.dcm")
            file.save(temp_path)

            logger.info(f"File diterima dan disimpan sementara: {temp_path}")

            # 2. Upload ke Orthanc
            with open(temp_path, 'rb') as f:
                r = requests.post(
                    f"{ORTHANC_URL}/instances",
                    auth=ORTHANC_AUTH,
                    data=f.read()
                )

            if r.status_code != 200:
                logger.error(f"Gagal upload ke Orthanc: {r.text}")
                return {"status": "error", "message": "Gagal upload ke Orthanc"}, 500

            orthanc_response = r.json()
            orthanc_instance_id = orthanc_response.get("ID")

            # 3. Ambil Study ID dari instance
            study_id = orthanc_response.get("ParentStudy")

            logger.info(f"Upload berhasil. InstanceID={orthanc_instance_id}, StudyID={study_id}")

            # 4. Hapus file sementara
            if os.path.exists(temp_path):
                os.remove(temp_path)

            return {
                "status": "success",
                "orthanc_instance_id": orthanc_instance_id,
                "orthanc_study_id": study_id
            }, 200

        except Exception as e:
            logger.error(f"Error upload-orth: {str(e)}")
            return {"status": "error", "message": str(e)}, 500


@app.route("/api/logs")
def get_logs():
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, 'r') as f: return "".join(f.readlines()[-30:])
    return "No logs."

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)