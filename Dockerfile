# Apify base image with Python, Playwright and Chromium preinstalled
FROM apify/actor-python-playwright:3.12

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . ./
RUN python -m compileall -q src/

CMD ["python", "-m", "src"]
