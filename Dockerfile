# Immagine per il NAS: contiene solo Python e le librerie.
# Il codice NON è copiato dentro: arriva da /volume1/timoni (montato in /app),
# così le modifiche sincronizzate da Synology Drive bastano un riavvio, senza ricostruire.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Europe/Rome

COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

WORKDIR /app
EXPOSE 5010
CMD ["python", "app.py"]
