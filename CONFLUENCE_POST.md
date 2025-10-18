# Integrating Zerobus Telemetry with Databricks Chatbot Applications

**Author:** Technical Documentation Team  
**Date:** October 2025  
**Tags:** `zerobus` `telemetry` `chatbot` `databricks-apps` `unity-catalog`

---

## 📋 Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [Implementation Guide](#implementation-guide)
- [Handling Connection Resilience](#handling-connection-resilience)
- [Monitoring & Analytics](#monitoring--analytics)
- [Best Practices](#best-practices)
- [Troubleshooting](#troubleshooting)
- [Code Examples](#code-examples)

---

## Overview

This document describes how to integrate **Databricks Zerobus SDK** with a production chatbot application to capture real-time telemetry data into Unity Catalog. Zerobus provides a high-performance, low-latency streaming ingestion path for telemetry data, enabling real-time monitoring, analytics, and model improvement workflows.

### What is Zerobus?

Zerobus is Databricks' high-performance telemetry ingestion service that:
- Streams data directly to Unity Catalog tables
- Provides microsecond-level latency for real-time analytics
- Supports protobuf-based schema definitions
- Handles millions of events per second
- Integrates seamlessly with Databricks workflows

### Use Case: Chatbot Telemetry

For AI chatbot applications, Zerobus enables:
- **Real-time conversation tracking**: Capture user messages and model responses
- **Performance monitoring**: Track response times and model latency
- **Quality feedback**: Collect thumbs up/down ratings for model improvement
- **Usage analytics**: Monitor user engagement and conversation patterns
- **Debug & troubleshooting**: Investigate issues with complete conversation context

---

## Architecture

### High-Level Architecture

```
┌─────────────────────┐
│   User Browser      │
│   (Web UI)          │
└──────────┬──────────┘
           │ HTTPS
           ▼
┌─────────────────────┐      ┌──────────────────────┐
│  Dash Application   │─────▶│  Model Serving       │
│  (Python)           │ REST │  Endpoint            │
│                     │◀─────│  (Foundation Model)  │
└──────────┬──────────┘      └──────────────────────┘
           │ gRPC
           │ (Protobuf)
           ▼
┌─────────────────────┐
│  Zerobus Server     │
│  (Streaming)        │
└──────────┬──────────┘
           │ Write
           ▼
┌─────────────────────┐
│  Unity Catalog      │
│  (Delta Table)      │
└─────────────────────┘
```

### Component Details

1. **Dash Application**: Python-based web UI handling user interactions
2. **Model Serving**: Databricks Foundation Model API endpoint
3. **Zerobus SDK**: Python SDK for streaming telemetry ingestion
4. **Unity Catalog**: Central data repository for telemetry analysis

---

## Implementation Guide

### Step 1: Define Protobuf Schema

Create a `.proto` file defining your telemetry structure:

```protobuf
syntax = "proto3";

message Chat {
    string telemetry_id = 1;
    string user_message = 2;
    string assistant_message = 3;
    int32 response_time_ms = 4;
}
```

**Key Considerations:**
- Field numbers must match Unity Catalog table schema
- Use appropriate data types (string, int32, int64, float, etc.)
- Keep messages under 1MB for optimal performance

### Step 2: Generate Python Code

Use the Zerobus CLI to auto-generate from your Unity Catalog table:

```bash
python -m zerobus.tools.generate_proto \
  --uc-endpoint "https://your-workspace.cloud.databricks.com" \
  --table "catalog.schema.telemetry_table" \
  --client-id "your-client-id" \
  --client-secret "your-client-secret" \
  --proto-msg "Chat" \
  --output "record.proto"

# Compile to Python
python -m grpc_tools.protoc \
  --python_out=. \
  --proto_path=. \
  record.proto
```

This generates `record_pb2.py` with Python classes.

### Step 3: Initialize Zerobus SDK

```python
import threading
from zerobus.sdk.sync import ZerobusSdk
from zerobus.sdk.shared import TableProperties
import record_pb2

# Global stream and lock
stream = None
stream_lock = threading.Lock()

def init_zerobus(force_reinit=False):
    """Initialize or reinitialize the Zerobus stream."""
    global stream
    
    with stream_lock:
        try:
            # Close existing stream if reinitializing
            if force_reinit and stream is not None:
                stream.close()
                stream = None
            
            # Create new stream
            if stream is None:
                sdk = ZerobusSdk(server_endpoint, workspace_url)
                table_properties = TableProperties(
                    table_name, 
                    record_pb2.Chat.DESCRIPTOR
                )
                stream = sdk.create_stream(
                    client_id, 
                    client_secret, 
                    table_properties
                )
                logger.info("Zerobus stream initialized")
                return True
        except Exception as e:
            logger.error(f"Failed to initialize Zerobus: {e}")
            return False

# Initialize at startup
init_zerobus()
```

**Important Notes:**
- Use thread locks for multi-threaded applications (like Dash)
- Initialize once at application startup
- Store stream globally for reuse across requests

### Step 4: Implement Telemetry Sending

```python
def send_telemetry(user_message, assistant_message, response_time_ms):
    """Send telemetry event to Zerobus."""
    global stream
    
    try:
        # Create protobuf record
        record = record_pb2.Chat(
            telemetry_id=str(uuid.uuid4()),
            user_message=user_message[:10000],  # Limit size
            assistant_message=assistant_message[:10000],
            response_time_ms=response_time_ms
        )
        
        # Ingest to Zerobus
        ack = stream.ingest_record(record)
        ack.wait_for_ack()
        
        logger.info(f"Telemetry sent: {ack}")
        return True
        
    except Exception as e:
        logger.warning(f"Failed to send telemetry: {e}")
        return False
```

---

## Handling Connection Resilience

### The Challenge

**Zerobus servers drop connections every ~1 hour** for maintenance and load balancing. Without proper handling, this causes:
- Lost telemetry data
- Application errors
- Poor user experience

### The Solution: Automatic Retry with Reconnection

Implement retry logic with automatic stream reinitialization:

```python
def send_telemetry_with_retry(record, max_retries=3):
    """Send telemetry with automatic retry and reconnection.
    
    Handles hourly Zerobus connection drops automatically.
    """
    global stream
    
    for attempt in range(max_retries):
        try:
            # Check if stream exists
            if stream is None:
                logger.info(f"Stream not initialized, initializing... (attempt {attempt + 1}/{max_retries})")
                if not init_zerobus(force_reinit=False):
                    time.sleep(1)
                    continue
            
            # Try to send
            ack = stream.ingest_record(record)
            ack.wait_for_ack()
            logger.info(f"Telemetry sent successfully: {ack}")
            return True
            
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Failed to send telemetry (attempt {attempt + 1}/{max_retries}): {error_msg}")
            
            # Detect closed stream errors
            if "closed" in error_msg.lower() or "connection" in error_msg.lower():
                logger.info("Stream appears closed, reinitializing...")
                if not init_zerobus(force_reinit=True):
                    time.sleep(1)
                    continue
            
            # Exponential backoff: 1s, 2s, 4s
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt
                logger.info(f"Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
    
    logger.error(f"Failed to send telemetry after {max_retries} attempts")
    return False
```

### Key Features

✅ **Automatic Detection**: Recognizes "closed stream" errors  
✅ **Smart Reconnection**: Reinitializes stream on connection failures  
✅ **Exponential Backoff**: 1s, 2s, 4s delays prevent server overload  
✅ **Thread-Safe**: Uses locks for concurrent access  
✅ **Zero Data Loss**: Retries ensure telemetry delivery  

### Usage in Application

```python
# In your chatbot handler
@app.callback(...)
def handle_chat_message(user_input):
    # Get model response
    response = query_model(user_input)
    response_time = calculate_response_time()
    
    # Send telemetry with retry
    record = record_pb2.Chat(
        telemetry_id=str(uuid.uuid4()),
        user_message=user_input[:10000],
        assistant_message=response[:10000],
        response_time_ms=response_time
    )
    send_telemetry_with_retry(record, max_retries=3)
    
    return response
```

---

## Monitoring & Analytics

### Log Monitoring

Monitor application logs for connection health:

```bash
# Successful telemetry
2025-10-17 10:23:45 INFO - Telemetry sent successfully to Zerobus: <ack>

# Connection drop detected
2025-10-17 11:00:12 WARNING - Failed to send telemetry (attempt 1/3): Cannot ingest records after stream is closed
2025-10-17 11:00:12 INFO - Stream appears closed, reinitializing...
2025-10-17 11:00:13 INFO - Zerobus stream initialized successfully

# Successful retry
2025-10-17 11:00:13 INFO - Telemetry sent successfully to Zerobus: <ack>
```

### Unity Catalog Queries

Query telemetry data for insights:

```sql
-- View recent conversations
SELECT 
    telemetry_id,
    user_message,
    assistant_message,
    response_time_ms,
    ingestion_time
FROM catalog.schema.chat_telemetry
WHERE ingestion_time > current_timestamp() - INTERVAL 1 DAY
ORDER BY ingestion_time DESC
LIMIT 100;

-- Average response times
SELECT 
    DATE(ingestion_time) as date,
    AVG(response_time_ms) as avg_response_time,
    MAX(response_time_ms) as max_response_time,
    COUNT(*) as total_messages
FROM catalog.schema.chat_telemetry
GROUP BY date
ORDER BY date DESC;

-- Most common user queries
SELECT 
    SUBSTRING(user_message, 1, 100) as query_sample,
    COUNT(*) as frequency
FROM catalog.schema.chat_telemetry
WHERE LENGTH(user_message) > 0
GROUP BY SUBSTRING(user_message, 1, 100)
ORDER BY frequency DESC
LIMIT 20;
```

### Real-Time Dashboard

Create dashboards in Databricks SQL for:
- Messages per minute/hour
- Average response times
- Error rates
- User feedback distribution
- Peak usage times

---

## Best Practices

### 1. Performance Optimization

**Message Size Limits**
```python
# Truncate large messages to prevent performance issues
user_message = user_input[:10000]  # Max 10KB
assistant_message = response[:10000]
```

**Async Sending** (Optional)
```python
import asyncio

async def send_telemetry_async(record):
    """Send telemetry without blocking main thread."""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, send_telemetry_with_retry, record)
```

### 2. Error Handling

**Don't Block User Experience**
```python
try:
    send_telemetry_with_retry(record)
except Exception as e:
    # Log but don't fail the request
    logger.warning(f"Telemetry failed (non-blocking): {e}")
    
# Always return response to user
return user_response
```

### 3. Data Privacy

**PII Handling**
```python
import re

def sanitize_message(message):
    """Remove PII from messages before logging."""
    # Remove emails
    message = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '[EMAIL]', message)
    # Remove phone numbers
    message = re.sub(r'\b\d{3}[-.]?\d{3}[-.]?\d{4}\b', '[PHONE]', message)
    return message

# Use in telemetry
record = record_pb2.Chat(
    user_message=sanitize_message(user_input),
    assistant_message=sanitize_message(response)
)
```

### 4. Resource Management

**Graceful Shutdown**
```python
import atexit

def cleanup_zerobus():
    """Close Zerobus stream on application shutdown."""
    global stream
    if stream is not None:
        try:
            stream.close()
            logger.info("Zerobus stream closed cleanly")
        except Exception as e:
            logger.error(f"Error closing stream: {e}")

# Register cleanup handler
atexit.register(cleanup_zerobus)
```

### 5. Configuration Management

**Use Environment Variables**
```python
import os

# Configuration
ZEROBUS_SERVER_ENDPOINT = os.getenv('ZEROBUS_SERVER_ENDPOINT')
ZEROBUS_TABLE = os.getenv('ZEROBUS_TABLE')
ZEROBUS_HOST = os.getenv('ZEROBUS_HOST')
CLIENT_ID = os.getenv('DATABRICKS_CLIENT_ID')
CLIENT_SECRET = os.getenv('DATABRICKS_CLIENT_SECRET')

# Validate configuration
assert ZEROBUS_SERVER_ENDPOINT, "ZEROBUS_SERVER_ENDPOINT must be set"
assert ZEROBUS_TABLE, "ZEROBUS_TABLE must be set"
```

---

## Troubleshooting

### Common Issues

#### Issue 1: "Cannot ingest records after stream is closed"

**Cause:** Hourly connection drop from Zerobus server  
**Solution:** Use retry logic with `force_reinit=True`

```python
# Handled automatically by send_telemetry_with_retry()
if "closed" in error_msg.lower():
    init_zerobus(force_reinit=True)
```

#### Issue 2: High Latency / Timeouts

**Cause:** Large message sizes or network issues  
**Solution:** 
- Truncate messages to reasonable size (< 10KB)
- Check network connectivity to Zerobus server
- Increase timeout values if needed

#### Issue 3: Authentication Failures

**Cause:** Invalid or expired OAuth credentials  
**Solution:**
```bash
# Verify credentials
databricks auth login --host https://your-workspace.cloud.databricks.com

# Test credentials
curl -H "Authorization: Bearer $DATABRICKS_TOKEN" \
  https://your-workspace.cloud.databricks.com/api/2.0/clusters/list
```

#### Issue 4: Schema Mismatch

**Cause:** Protobuf schema doesn't match Unity Catalog table  
**Solution:** Regenerate protobuf from table schema
```bash
python -m zerobus.tools.generate_proto \
  --uc-endpoint "$WORKSPACE_URL" \
  --table "$TABLE_NAME" \
  --client-id "$CLIENT_ID" \
  --client-secret "$CLIENT_SECRET" \
  --proto-msg "Chat" \
  --output "record.proto"
```

---

## Code Examples

### Complete Integration Example

```python
import logging
import os
import time
import uuid
import threading
from zerobus.sdk.sync import ZerobusSdk
from zerobus.sdk.shared import TableProperties
import record_pb2

# Configuration
ZEROBUS_SERVER_ENDPOINT = os.getenv('ZEROBUS_SERVER_ENDPOINT')
ZEROBUS_TABLE = os.getenv('ZEROBUS_TABLE')
ZEROBUS_HOST = os.getenv('ZEROBUS_HOST')
CLIENT_ID = os.getenv('DATABRICKS_CLIENT_ID')
CLIENT_SECRET = os.getenv('DATABRICKS_CLIENT_SECRET')

# Global state
stream = None
stream_lock = threading.Lock()
logger = logging.getLogger(__name__)

def init_zerobus(force_reinit=False):
    """Initialize Zerobus stream."""
    global stream
    
    with stream_lock:
        try:
            if force_reinit and stream is not None:
                stream.close()
                stream = None
            
            if stream is None:
                sdk = ZerobusSdk(ZEROBUS_SERVER_ENDPOINT, ZEROBUS_HOST)
                table_properties = TableProperties(
                    ZEROBUS_TABLE, 
                    record_pb2.Chat.DESCRIPTOR
                )
                stream = sdk.create_stream(
                    CLIENT_ID, 
                    CLIENT_SECRET, 
                    table_properties
                )
                logger.info("Zerobus stream initialized")
                return True
        except Exception as e:
            logger.error(f"Failed to initialize Zerobus: {e}")
            return False

def send_telemetry_with_retry(record, max_retries=3):
    """Send telemetry with retry logic."""
    global stream
    
    for attempt in range(max_retries):
        try:
            if stream is None:
                if not init_zerobus():
                    time.sleep(1)
                    continue
            
            ack = stream.ingest_record(record)
            ack.wait_for_ack()
            logger.info(f"Telemetry sent: {ack}")
            return True
            
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1}/{max_retries} failed: {e}")
            
            if "closed" in str(e).lower():
                init_zerobus(force_reinit=True)
            
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
    
    return False

def track_chat_interaction(user_message, assistant_message, response_time_ms):
    """Track a chat interaction to Zerobus."""
    try:
        record = record_pb2.Chat(
            telemetry_id=str(uuid.uuid4()),
            user_message=user_message[:10000],
            assistant_message=assistant_message[:10000],
            response_time_ms=response_time_ms
        )
        send_telemetry_with_retry(record)
    except Exception as e:
        logger.warning(f"Failed to track interaction: {e}")

# Initialize at module load
init_zerobus()
```

---

## Summary

Integrating Zerobus with chatbot applications provides:

✅ **Real-time telemetry**: Immediate insight into user interactions  
✅ **Reliable delivery**: Automatic retry and reconnection handling  
✅ **Scalable**: Handles high-volume production workloads  
✅ **Unity Catalog integration**: Central data repository for analytics  
✅ **Performance**: Microsecond-level latency for streaming data  

### Key Takeaways

1. **Use retry logic** to handle hourly connection drops
2. **Implement thread-safe** stream management for concurrent applications
3. **Truncate large messages** to maintain performance
4. **Monitor logs** for connection health and retry patterns
5. **Query Unity Catalog** for real-time analytics and insights

### Next Steps

- Deploy chatbot application with Zerobus integration
- Set up monitoring dashboards in Databricks SQL
- Configure alerts for telemetry failures
- Analyze conversation data for model improvements
- Implement feedback loop based on telemetry insights

---

## References

- [Databricks Zerobus Documentation](https://docs.databricks.com/zerobus/)
- [Unity Catalog Documentation](https://docs.databricks.com/unity-catalog/)
- [Databricks Apps Documentation](https://docs.databricks.com/apps/)
- [Protocol Buffers Guide](https://protobuf.dev/)

---

**Questions or Issues?**  
Contact the Databricks Platform team or file a ticket in the internal support portal.

