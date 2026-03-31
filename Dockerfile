FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Build verification (from verification chain):
# "Is the code on the server the same code I see locally?"
RUN echo "=== V5 Build Verification ===" \
    && echo "Files:" && find /app -name "*.py" | wc -l \
    && echo "oracle_engine.py lines:" && wc -l < /app/engine/oracle_engine.py \
    && echo "run.py lines:" && wc -l < /app/run.py \
    && python -c "from engine.oracle_engine import OracleEngine, compute_settlement; print('✓ OracleEngine imports OK')" \
    && python -c "from engine.chainlink import ChainlinkTracker; print('✓ ChainlinkTracker imports OK')" \
    && python -c "from engine.config import DEFAULT_CONFIG; print(f'✓ Config OK ({len(DEFAULT_CONFIG)} params)')" \
    && echo "=== Build OK ==="

HEALTHCHECK --interval=60s --timeout=10s --retries=3 \
  CMD python -c "print('alive')" || exit 1

CMD ["python", "run.py"]
