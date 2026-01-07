# Gunakan image python yang ringan
FROM python:3.10-slim

# Install dependensi sistem (DCMTK untuk dcmodify)
RUN apt-get update && apt-get install -y \
    dcmtk \
    && rm -rf /var/lib/apt/lists/*

# Set direktori kerja di dalam container
WORKDIR /app

# Copy file requirements dan install library python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy seluruh kode aplikasi ke dalam container
COPY . .

# Buat folder temp dan log agar tidak error
RUN mkdir -p temp_dicom

# Port yang digunakan Flask
EXPOSE 5000

# Jalankan aplikasi menggunakan gunicorn untuk production (lebih stabil dibanding flask run)
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]