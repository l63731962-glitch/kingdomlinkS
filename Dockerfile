FROM python:3.11-slim
WORKDIR /app
COPY air_neomap/A.I.R.-NEOMAP/ .
RUN pip install --no-cache-dir -r requirements.txt
ENV DATABASE_URL=sqlite:///neomap_v2.db
ENV PORT=8080
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "2", "run:app"]
