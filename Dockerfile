FROM python:3.12-slim

WORKDIR /app

# Instala dependencias del sistema necesarias para opencv y django
RUN apt-get update && apt-get install -y \
    build-essential \
    cmake \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Copiamos requirements primero para aprovechar la cache de Docker
COPY ./requirements.txt ./

# Instala dependencias Python
RUN pip install --no-cache-dir -r requirements.txt

# Copiamos el resto del código
COPY ./ ./

# Comando para correr el servidor
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
