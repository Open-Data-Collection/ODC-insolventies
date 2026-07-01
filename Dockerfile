FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
COPY vendor/ /tmp/vendor/
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# One image, three modes. Nomad picks the module per component:
#   args = ["src.scheduler"]  # scheduler.nomad.hcl (discovery)
#   args = ["src.worker"]     # worker.nomad.hcl (scrape)
#   args = ["src.processor"]  # processor.nomad.hcl (fan-out)
ENTRYPOINT ["python", "-m"]
CMD ["src.worker"]
