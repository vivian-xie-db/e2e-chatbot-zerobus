import logging
import os
import streamlit as st
from model_serving_utils import (
    endpoint_supports_feedback, 
    query_endpoint, 
    query_endpoint_stream, 
    _get_endpoint_task_type,
)
from collections import OrderedDict
from messages import UserMessage, AssistantResponse, render_message
from psycopg import sql
from psycopg_pool import ConnectionPool
import time
from databricks.sdk import WorkspaceClient
import dotenv
dotenv.load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Database connection setup
workspace_client = WorkspaceClient()
postgres_password = None
last_password_refresh = 0
connection_pool = None

def refresh_oauth_token():
    """Refresh OAuth token if expired."""
    global postgres_password, last_password_refresh
    if postgres_password is None or time.time() - last_password_refresh > 900:
        logger.info("Refreshing PostgreSQL OAuth token")
        try:
            postgres_password = workspace_client.config.oauth_token().access_token
            last_password_refresh = time.time()
        except Exception as e:
            logger.error(f"Failed to refresh OAuth token: {str(e)}")
            st.error(f"❌ Failed to refresh OAuth token: {str(e)}")

def get_connection_pool():
    """Get or create the connection pool."""
    global connection_pool
    if connection_pool is None:
        refresh_oauth_token()
        conn_string = (
            f"dbname={os.getenv('PGDATABASE')} "
            f"user={os.getenv('PGUSER')} "
            f"password={postgres_password} "
            f"host={os.getenv('PGHOST')} "
            f"port={os.getenv('PGPORT')} "
            f"sslmode={os.getenv('PGSSLMODE', 'require')} "
            f"application_name={os.getenv('PGAPPNAME')}"
        )
        connection_pool = ConnectionPool(conn_string, min_size=2, max_size=10)
    return connection_pool

def get_connection():
    """Get a connection from the pool."""
    global connection_pool
    
    # Recreate pool if token expired
    if postgres_password is None or time.time() - last_password_refresh > 900:
        if connection_pool:
            connection_pool.close()
            connection_pool = None
    
    return get_connection_pool().connection()

def get_schema_name():
    """Get the schema name in the format {PGAPPNAME}_schema_{PGUSER}."""
    pgappname = os.getenv("PGAPPNAME", "chatbot_app")
    pguser = os.getenv("PGUSER", "").replace('-', '')
    return f"{pgappname}_schema_{pguser}"

def init_database():
    """Initialize database schema and table."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                schema_name = get_schema_name()
                
                cur.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema_name)))
                cur.execute(sql.SQL("""
                    CREATE TABLE IF NOT EXISTS {}.chat_history (
                        id SERIAL PRIMARY KEY,
                        user_message TEXT NOT NULL,
                        assistant_response TEXT NOT NULL,
                        request_id TEXT,
                        endpoint_name TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """).format(sql.Identifier(schema_name)))
                conn.commit()
                logger.info(f"Database initialized with schema: {schema_name}")
                return True
    except Exception as e:
        logger.error(f"Failed to initialize database: {str(e)}")
        st.warning(f"⚠️ Database initialization failed: {str(e)}. Continuing without persistence.")
        return False

def save_chat_interaction(user_message, assistant_response, request_id=None):
    """Save a chat interaction to the database."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                schema = get_schema_name()
                
                # Convert assistant response messages to a single string
                if isinstance(assistant_response, list):
                    response_text = "\n".join([
                        msg.get("content", "") for msg in assistant_response if msg.get("content")
                    ])
                else:
                    response_text = str(assistant_response)
                
                cur.execute(
                    sql.SQL("INSERT INTO {}.chat_history (user_message, assistant_response, request_id, endpoint_name) VALUES (%s, %s, %s, %s)").format(
                        sql.Identifier(schema)
                    ),
                    (user_message, response_text, request_id, SERVING_ENDPOINT)
                )
                conn.commit()
                logger.info(f"Saved chat interaction to database (request_id: {request_id})")
    except Exception as e:
        logger.error(f"Failed to save chat interaction: {str(e)}")
        # Don't show error to user, just log it

def get_chat_history():
    """Retrieve chat history from database."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                schema = get_schema_name()
                cur.execute(
                    sql.SQL("SELECT user_message, assistant_response, request_id, endpoint_name, created_at FROM {}.chat_history ORDER BY created_at DESC LIMIT 50").format(
                        sql.Identifier(schema)
                    )
                )
                return cur.fetchall()
    except Exception as e:
        logger.error(f"Failed to retrieve chat history: {str(e)}")
        return []

SERVING_ENDPOINT = os.getenv('SERVING_ENDPOINT')
assert SERVING_ENDPOINT, \
    ("Unable to determine serving endpoint to use for chatbot app. If developing locally, "
     "set the SERVING_ENDPOINT environment variable to the name of your serving endpoint. If "
     "deploying to a Databricks app, include a serving endpoint resource named "
     "'serving_endpoint' with CAN_QUERY permissions, as described in "
     "https://docs.databricks.com/aws/en/generative-ai/agent-framework/chat-app#deploy-the-databricks-app")

ENDPOINT_SUPPORTS_FEEDBACK = endpoint_supports_feedback(SERVING_ENDPOINT)

def reduce_chat_agent_chunks(chunks):
    """
    Reduce a list of ChatAgentChunk objects corresponding to a particular
    message into a single ChatAgentMessage
    """
    deltas = [chunk.delta for chunk in chunks]
    first_delta = deltas[0]
    result_msg = first_delta
    msg_contents = []
    
    # Accumulate tool calls properly
    tool_call_map = {}  # Map call_id to tool call for accumulation
    
    for delta in deltas:
        # Handle content
        if delta.content:
            msg_contents.append(delta.content)
            
        # Handle tool calls
        if hasattr(delta, 'tool_calls') and delta.tool_calls:
            for tool_call in delta.tool_calls:
                call_id = getattr(tool_call, 'id', None)
                tool_type = getattr(tool_call, 'type', "function")
                function_info = getattr(tool_call, 'function', None)
                if function_info:
                    func_name = getattr(function_info, 'name', "")
                    func_args = getattr(function_info, 'arguments', "")
                else:
                    func_name = ""
                    func_args = ""
                
                if call_id:
                    if call_id not in tool_call_map:
                        # New tool call
                        tool_call_map[call_id] = {
                            "id": call_id,
                            "type": tool_type,
                            "function": {
                                "name": func_name,
                                "arguments": func_args
                            }
                        }
                    else:
                        # Accumulate arguments for existing tool call
                        existing_args = tool_call_map[call_id]["function"]["arguments"]
                        tool_call_map[call_id]["function"]["arguments"] = existing_args + func_args

                        # Update function name if provided
                        if func_name:
                            tool_call_map[call_id]["function"]["name"] = func_name

        # Handle tool call IDs (for tool response messages)
        if hasattr(delta, 'tool_call_id') and delta.tool_call_id:
            result_msg = result_msg.model_copy(update={"tool_call_id": delta.tool_call_id})
    
    # Convert tool call map back to list
    if tool_call_map:
        accumulated_tool_calls = list(tool_call_map.values())
        result_msg = result_msg.model_copy(update={"tool_calls": accumulated_tool_calls})
    
    result_msg = result_msg.model_copy(update={"content": "".join(msg_contents)})
    return result_msg



# --- Init state ---
if "history" not in st.session_state:
    st.session_state.history = []

if "db_initialized" not in st.session_state:
    st.session_state.db_initialized = init_database()

if "viewing_history" not in st.session_state:
    st.session_state.viewing_history = None

st.title("🧱 Chatbot App")
st.write("A basic chatbot using your own serving endpoint.")
st.write(f"Endpoint name: `{SERVING_ENDPOINT}`")
if st.session_state.db_initialized:
    st.success("✅ Database connected - chat history will be saved")
else:
    st.warning("⚠️ Running without database persistence")

# Add sidebar with chat history viewer
with st.sidebar:
    st.header("📜 Chat History")
    if st.session_state.db_initialized:
        col1, col2 = st.columns([1, 1])
        with col1:
            if st.button("🔄 Refresh", use_container_width=True):
                st.rerun()
        with col2:
            if st.button("📝 New Chat", use_container_width=True):
                st.session_state.viewing_history = None
                st.rerun()
        
        if st.session_state.viewing_history is not None:
            st.info("👁️ Viewing historical conversation")
        
        st.divider()
        
        history_data = get_chat_history()
        if history_data:
            st.caption(f"Showing {len(history_data)} recent conversations")
            for idx, (user_msg, assistant_msg, req_id, endpoint, created_at) in enumerate(history_data):
                # Create a container for each history item
                with st.container():
                    # Show preview of the conversation
                    preview = user_msg[:50] + "..." if len(user_msg) > 50 else user_msg
                    
                    # Button to view this conversation
                    if st.button(
                        f"💬 {created_at.strftime('%Y-%m-%d %H:%M')}\n{preview}",
                        key=f"view_history_{idx}",
                        use_container_width=True
                    ):
                        st.session_state.viewing_history = {
                            "user_msg": user_msg,
                            "assistant_msg": assistant_msg,
                            "req_id": req_id,
                            "endpoint": endpoint,
                            "created_at": created_at
                        }
                        st.rerun()
        else:
            st.info("No chat history yet. Start a conversation!")
    else:
        st.info("Database not available")



# --- Render chat history or selected conversation ---
if st.session_state.viewing_history is not None:
    # Display the selected historical conversation
    hist = st.session_state.viewing_history
    
    st.info(f"📅 Viewing conversation from {hist['created_at'].strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Display user message
    with st.chat_message("user"):
        st.markdown(hist["user_msg"])
    
    # Display assistant response
    with st.chat_message("assistant"):
        st.markdown(hist["assistant_msg"])
    
    # Show metadata
    with st.expander("ℹ️ Conversation Details"):
        st.write(f"**Endpoint:** {hist['endpoint']}")
        if hist['req_id']:
            st.write(f"**Request ID:** {hist['req_id']}")
        st.write(f"**Timestamp:** {hist['created_at'].strftime('%Y-%m-%d %H:%M:%S')}")
    
    # Show message that chat input is disabled
    st.info("💡 Click '📝 New Chat' in the sidebar to start a new conversation")
else:
    # Display current live chat history
    for i, element in enumerate(st.session_state.history):
        element.render(i)

def query_endpoint_and_render(task_type, input_messages):
    """Handle streaming response based on task type."""
    if task_type == "agent/v1/responses":
        return query_responses_endpoint_and_render(input_messages)
    elif task_type == "agent/v2/chat":
        return query_chat_agent_endpoint_and_render(input_messages)
    else:  # chat/completions
        return query_chat_completions_endpoint_and_render(input_messages)


def query_chat_completions_endpoint_and_render(input_messages):
    """Handle ChatCompletions streaming format."""
    with st.chat_message("assistant"):
        response_area = st.empty()
        response_area.markdown("_Thinking..._")
        
        accumulated_content = ""
        request_id = None
        
        try:
            for chunk in query_endpoint_stream(
                endpoint_name=SERVING_ENDPOINT,
                messages=input_messages,
                return_traces=ENDPOINT_SUPPORTS_FEEDBACK
            ):
                if "choices" in chunk and chunk["choices"]:
                    delta = chunk["choices"][0].get("delta", {})
                    content = delta.get("content", "")
                    if content:
                        accumulated_content += content
                        response_area.markdown(accumulated_content)
                
                if "databricks_output" in chunk:
                    req_id = chunk["databricks_output"].get("databricks_request_id")
                    if req_id:
                        request_id = req_id
            
            return AssistantResponse(
                messages=[{"role": "assistant", "content": accumulated_content}],
                request_id=request_id
            )
        except Exception:
            response_area.markdown("_Ran into an error. Retrying without streaming..._")
            messages, request_id = query_endpoint(
                endpoint_name=SERVING_ENDPOINT,
                messages=input_messages,
                return_traces=ENDPOINT_SUPPORTS_FEEDBACK
            )
            response_area.empty()
            with response_area.container():
                for message in messages:
                    render_message(message)
            return AssistantResponse(messages=messages, request_id=request_id)


def query_chat_agent_endpoint_and_render(input_messages):
    """Handle ChatAgent streaming format."""
    from mlflow.types.agent import ChatAgentChunk
    
    with st.chat_message("assistant"):
        response_area = st.empty()
        response_area.markdown("_Thinking..._")
        
        message_buffers = OrderedDict()
        request_id = None
        
        try:
            for raw_chunk in query_endpoint_stream(
                endpoint_name=SERVING_ENDPOINT,
                messages=input_messages,
                return_traces=ENDPOINT_SUPPORTS_FEEDBACK
            ):
                response_area.empty()
                chunk = ChatAgentChunk.model_validate(raw_chunk)
                delta = chunk.delta
                message_id = delta.id

                req_id = raw_chunk.get("databricks_output", {}).get("databricks_request_id")
                if req_id:
                    request_id = req_id
                if message_id not in message_buffers:
                    message_buffers[message_id] = {
                        "chunks": [],
                        "render_area": st.empty(),
                    }
                message_buffers[message_id]["chunks"].append(chunk)
                
                partial_message = reduce_chat_agent_chunks(message_buffers[message_id]["chunks"])
                render_area = message_buffers[message_id]["render_area"]
                message_content = partial_message.model_dump_compat(exclude_none=True)
                with render_area.container():
                    render_message(message_content)
            
            messages = []
            for msg_id, msg_info in message_buffers.items():
                messages.append(reduce_chat_agent_chunks(msg_info["chunks"]))
            
            return AssistantResponse(
                messages=[message.model_dump_compat(exclude_none=True) for message in messages],
                request_id=request_id
            )
        except Exception:
            response_area.markdown("_Ran into an error. Retrying without streaming..._")
            messages, request_id = query_endpoint(
                endpoint_name=SERVING_ENDPOINT,
                messages=input_messages,
                return_traces=ENDPOINT_SUPPORTS_FEEDBACK
            )
            response_area.empty()
            with response_area.container():
                for message in messages:
                    render_message(message)
            return AssistantResponse(messages=messages, request_id=request_id)


def query_responses_endpoint_and_render(input_messages):
    """Handle ResponsesAgent streaming format using MLflow types."""
    from mlflow.types.responses import ResponsesAgentStreamEvent
    
    with st.chat_message("assistant"):
        response_area = st.empty()
        response_area.markdown("_Thinking..._")
        
        # Track all the messages that need to be rendered in order
        all_messages = []
        request_id = None

        try:
            for raw_event in query_endpoint_stream(
                endpoint_name=SERVING_ENDPOINT,
                messages=input_messages,
                return_traces=ENDPOINT_SUPPORTS_FEEDBACK
            ):
                # Extract databricks_output for request_id
                if "databricks_output" in raw_event:
                    req_id = raw_event["databricks_output"].get("databricks_request_id")
                    if req_id:
                        request_id = req_id
                
                # Parse using MLflow streaming event types, similar to ChatAgentChunk
                if "type" in raw_event:
                    event = ResponsesAgentStreamEvent.model_validate(raw_event)
                    
                    if hasattr(event, 'item') and event.item:
                        item = event.item  # This is a dict, not a parsed object
                        
                        if item.get("type") == "message":
                            # Extract text content from message if present
                            content_parts = item.get("content", [])
                            for content_part in content_parts:
                                if content_part.get("type") == "output_text":
                                    text = content_part.get("text", "")
                                    if text:
                                        all_messages.append({
                                            "role": "assistant",
                                            "content": text
                                        })
                            
                        elif item.get("type") == "function_call":
                            # Tool call
                            call_id = item.get("call_id")
                            function_name = item.get("name")
                            arguments = item.get("arguments", "")
                            
                            # Add to messages for history
                            all_messages.append({
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [{
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": function_name,
                                        "arguments": arguments
                                    }
                                }]
                            })
                            
                        elif item.get("type") == "function_call_output":
                            # Tool call output/result
                            call_id = item.get("call_id")
                            output = item.get("output", "")
                            
                            # Add to messages for history
                            all_messages.append({
                                "role": "tool",
                                "content": output,
                                "tool_call_id": call_id
                            })
                
                # Update the display by rendering all accumulated messages
                if all_messages:
                    with response_area.container():
                        for msg in all_messages:
                            render_message(msg)

            return AssistantResponse(messages=all_messages, request_id=request_id)
        except Exception:
            response_area.markdown("_Ran into an error. Retrying without streaming..._")
            messages, request_id = query_endpoint(
                endpoint_name=SERVING_ENDPOINT,
                messages=input_messages,
                return_traces=ENDPOINT_SUPPORTS_FEEDBACK
            )
            response_area.empty()
            with response_area.container():
                for message in messages:
                    render_message(message)
            return AssistantResponse(messages=messages, request_id=request_id)




# --- Chat input (must run BEFORE rendering messages) ---
# Only show chat input when not viewing history
if st.session_state.viewing_history is None:
    prompt = st.chat_input("Ask a question")
    if prompt:
        # Get the task type for this endpoint
        task_type = _get_endpoint_task_type(SERVING_ENDPOINT)
        
        # Add user message to chat history
        user_msg = UserMessage(content=prompt)
        st.session_state.history.append(user_msg)
        user_msg.render(len(st.session_state.history) - 1)

        # Convert history to standard chat message format for the query methods
        input_messages = [msg for elem in st.session_state.history for msg in elem.to_input_messages()]
        
        # Handle the response using the appropriate handler
        assistant_response = query_endpoint_and_render(task_type, input_messages)
        
        # Add assistant response to history
        st.session_state.history.append(assistant_response)
        
        # Save to database if initialized
        if st.session_state.db_initialized:
            save_chat_interaction(
                user_message=prompt,
                assistant_response=assistant_response.messages,
                request_id=assistant_response.request_id
            )
else:
    # Disable chat input when viewing history
    st.chat_input("Ask a question", disabled=True)
