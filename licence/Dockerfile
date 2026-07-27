# The licence server. Deliberately tiny and separate from the webapp image:
# it has to stay up while the video app is being rebuilt, because a customer
# activating a fresh purchase does not care that we are deploying.
FROM python:3.12-slim

WORKDIR /srv

COPY licence/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY licence/ ./licence/

EXPOSE 8600
CMD ["uvicorn", "licence.app:app", "--host", "0.0.0.0", "--port", "8600"]
