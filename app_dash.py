import logging
import os
import time
import uuid
import threading
from datetime import datetime
from dash import Dash, html, dcc, Input, Output, State, callback, ctx, no_update, ALL, MATCH
import dash_bootstrap_components as dbc
from model_serving_utils import (
    endpoint_supports_feedback,
    query_endpoint,
    _get_endpoint_task_type,
)
import record_pb2
import dotenv
import json
from zerobus.sdk.sync import ZerobusSdk
from zerobus.sdk.shared import TableProperties


dotenv.load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SERVING_ENDPOINT = os.getenv('SERVING_ENDPOINT')
assert SERVING_ENDPOINT, "SERVING_ENDPOINT environment variable must be set"

ENDPOINT_SUPPORTS_FEEDBACK = endpoint_supports_feedback(SERVING_ENDPOINT)

server_endpoint = os.getenv('ZEROBUS_SERVER_ENDPOINT')
workspace_url = os.getenv('ZEROBUS_HOST')
table_name = os.getenv('ZEROBUS_TABLE')
client_id = os.getenv('DATABRICKS_CLIENT_ID')
client_secret = os.getenv('DATABRICKS_CLIENT_SECRET')

print(f"Server endpoint: {server_endpoint}")
print(f"Workspace URL: {workspace_url}")
print(f"Table name: {table_name}")
print(f"Client ID: {client_id}")
print(f"Client secret: {client_secret}")

stream = None
stream_initialized = True
stream_lock = threading.Lock()

# Initialize SDK
def init_zerobus(force_reinit=False):
    """Initialize or reinitialize the Zerobus stream.
    
    Args:
        force_reinit: If True, close existing stream and create new one
    """
    global stream, stream_initialized
    
    with stream_lock:
        try:
            # Close existing stream if reinitializing
            if force_reinit and stream is not None:
                try:
                    stream.close()
                    logger.info("Closed existing Zerobus stream")
                except Exception as e:
                    logger.warning(f"Error closing stream: {str(e)}")
                stream = None
            
            # Create new stream if needed
            if stream is None or force_reinit:
                logger.info("Initializing Zerobus stream...")
                sdk = ZerobusSdk(server_endpoint, workspace_url)
                table_properties = TableProperties(table_name, record_pb2.Chat.DESCRIPTOR)
                stream = sdk.create_stream(client_id, client_secret, table_properties)
                stream_initialized = True
                logger.info("Zerobus stream initialized successfully")
                return True
            return True
        except Exception as e:
            logger.error(f"Failed to initialize Zerobus: {str(e)}")
            stream = None
            stream_initialized = False
            return False

def send_telemetry_with_retry(record, max_retries=3):
    """Send telemetry with automatic retry and reconnection.
    
    Args:
        record: The protobuf record to send
        max_retries: Maximum number of retry attempts
    
    Returns:
        True if successful, False otherwise
    """
    global stream
    
    for attempt in range(max_retries):
        try:
            # Check if stream exists and is valid
            if stream is None:
                logger.info(f"Stream not initialized, initializing... (attempt {attempt + 1}/{max_retries})")
                if not init_zerobus(force_reinit=False):
                    time.sleep(1)  # Wait before retry
                    continue
            
            # Try to send the record
            ack = stream.ingest_record(record)
            ack.wait_for_ack()
            logger.info(f"Telemetry sent successfully to Zerobus: {ack}")
            return True
            
        except Exception as e:
            error_msg = str(e)
            logger.warning(f"Failed to send telemetry (attempt {attempt + 1}/{max_retries}): {error_msg}")
            
            # Check if error is due to closed stream
            if "closed" in error_msg.lower() or "connection" in error_msg.lower():
                logger.info("Stream appears closed, reinitializing...")
                # Force reinitialize on next attempt
                if not init_zerobus(force_reinit=True):
                    time.sleep(1)  # Wait before retry
                    continue
            
            # Wait before retry (exponential backoff)
            if attempt < max_retries - 1:
                wait_time = 2 ** attempt  # 1s, 2s, 4s
                logger.info(f"Waiting {wait_time}s before retry...")
                time.sleep(wait_time)
    
    logger.error(f"Failed to send telemetry after {max_retries} attempts")
    return False

# Initialize telemetry at startup
init_zerobus()

# Default welcome text
DEFAULT_WELCOME_TITLE = "🧱 Chatbot App"
DEFAULT_WELCOME_DESCRIPTION = "Chat with your AI model using a beautiful, modern interface."

# Default suggestion questions
DEFAULT_SUGGESTIONS = [
    "Hello! How can you help me?",
    "What can you do?",
    "Tell me something interesting",
    "Explain how AI works"
]

# Initialize Dash app
app = Dash(
    __name__,
    external_stylesheets=[dbc.themes.BOOTSTRAP],
    suppress_callback_exceptions=True
)

# App layout
app.layout = html.Div([
    # Top navigation bar
    html.Div([
        # Left component
        html.Div([
            # Nav left
            html.Div([
                html.Button([
                    html.Img(src="assets/menu_icon.svg", className="menu-icon")
                ], id="sidebar-toggle", className="nav-button"),
                html.Button([
                    html.Img(src="assets/plus_icon.svg", className="new-chat-icon")
                ], id="new-chat-button", className="nav-button", disabled=False),
                html.Button([
                    html.Img(src="assets/plus_icon.svg", className="new-chat-icon"),
                    html.Div("New chat", className="new-chat-text")
                ], id="sidebar-new-chat-button", className="new-chat-button", disabled=False)
            ], id="nav-left", className="nav-left"),
            
            # Sidebar
            html.Div([
                html.Div([
                    html.Div("Your conversations", className="sidebar-header-text"),
                ], className="sidebar-header"),
                html.Div([], className="chat-list", id="chat-list")
            ], id="sidebar", className="sidebar")
        ], id="left-component", className="left-component"),
        
        html.Div([
            html.Div("Chatbot Space", id="logo-container", className="logo-container")
        ], className="nav-center"),
        html.Div([
            html.Div(f"Endpoint: {SERVING_ENDPOINT[:20]}...", className="endpoint-info")
        ], className="nav-right")
    ], className="top-nav"),
    
    # Main content area
    html.Div([
        html.Div([
            # Chat content
            html.Div([
                # Welcome container
                html.Div([
                    # Title
                    html.Div(id="welcome-title", className="welcome-message", 
                            children=DEFAULT_WELCOME_TITLE),
                    
                    html.Div(id="welcome-description", 
                            className="welcome-message-description",
                            children=DEFAULT_WELCOME_DESCRIPTION),
                    
                    # Suggestion buttons
                    html.Div([
                        html.Button([
                            html.Div(className="suggestion-icon"),
                            html.Div(DEFAULT_SUGGESTIONS[0], className="suggestion-text", 
                                   id="suggestion-1-text")
                        ], id="suggestion-1", className="suggestion-button"),
                        html.Button([
                            html.Div(className="suggestion-icon"),
                            html.Div(DEFAULT_SUGGESTIONS[1], className="suggestion-text", 
                                   id="suggestion-2-text")
                        ], id="suggestion-2", className="suggestion-button"),
                        html.Button([
                            html.Div(className="suggestion-icon"),
                            html.Div(DEFAULT_SUGGESTIONS[2], className="suggestion-text", 
                                   id="suggestion-3-text")
                        ], id="suggestion-3", className="suggestion-button"),
                        html.Button([
                            html.Div(className="suggestion-icon"),
                            html.Div(DEFAULT_SUGGESTIONS[3], className="suggestion-text", 
                                   id="suggestion-4-text")
                        ], id="suggestion-4", className="suggestion-button")
                    ], className="suggestion-buttons")
                ], id="welcome-container", className="welcome-container visible"),
                
                # Chat messages
                html.Div([], id="chat-messages", className="chat-messages"),
            ], id="chat-content", className="chat-content"),
            
            # Input area
            html.Div([
                html.Div([
                    dcc.Input(
                        id="chat-input-fixed",
                        placeholder="Ask your question...",
                        className="chat-input",
                        type="text",
                        disabled=False
                    ),
                    html.Div([
                        html.Button(
                            id="send-button-fixed", 
                            className="input-button send-button",
                            disabled=False
                        )
                    ], className="input-buttons-right"),
                    html.Div("Processing your request...", 
                            id="query-tooltip", 
                            className="query-tooltip hidden")
                ], id="fixed-input-container", className="fixed-input-container"),
                html.Div("Always review the accuracy of responses.", className="disclaimer-fixed")
            ], id="fixed-input-wrapper", className="fixed-input-wrapper"),
        ], id="chat-container", className="chat-container"),
    ], id="main-content", className="main-content"),
    
    html.Div(id='dummy-output'),
    dcc.Store(id="chat-trigger", data={"trigger": False, "message": ""}),
    dcc.Store(id="chat-history-store", data=[]),
    dcc.Store(id="query-running-store", data=False),
    dcc.Store(id="session-store", data={"current_session": None})
])


# First callback: Handle inputs and show thinking indicator
@app.callback(
    [Output("chat-messages", "children", allow_duplicate=True),
     Output("chat-input-fixed", "value", allow_duplicate=True),
     Output("welcome-container", "className", allow_duplicate=True),
     Output("chat-trigger", "data", allow_duplicate=True),
     Output("query-running-store", "data", allow_duplicate=True),
     Output("chat-list", "children", allow_duplicate=True),
     Output("chat-history-store", "data", allow_duplicate=True),
     Output("session-store", "data", allow_duplicate=True)],
    [Input("suggestion-1", "n_clicks"),
     Input("suggestion-2", "n_clicks"),
     Input("suggestion-3", "n_clicks"),
     Input("suggestion-4", "n_clicks"),
     Input("send-button-fixed", "n_clicks"),
     Input("chat-input-fixed", "n_submit")],
    [State("suggestion-1-text", "children"),
     State("suggestion-2-text", "children"),
     State("suggestion-3-text", "children"),
     State("suggestion-4-text", "children"),
     State("chat-input-fixed", "value"),
     State("chat-messages", "children"),
     State("welcome-container", "className"),
     State("chat-list", "children"),
     State("chat-history-store", "data"),
     State("session-store", "data")],
    prevent_initial_call=True
)
def handle_all_inputs(s1_clicks, s2_clicks, s3_clicks, s4_clicks, send_clicks, submit_clicks,
                     s1_text, s2_text, s3_text, s4_text, input_value, current_messages,
                     welcome_class, current_chat_list, chat_history, session_data):
    triggered_ctx = ctx
    if not triggered_ctx.triggered:
        return [no_update] * 8

    trigger_id = triggered_ctx.triggered[0]["prop_id"].split(".")[0]
    
    # Handle suggestion buttons
    suggestion_map = {
        "suggestion-1": s1_text,
        "suggestion-2": s2_text,
        "suggestion-3": s3_text,
        "suggestion-4": s4_text
    }
    
    # Get the user input
    if trigger_id in suggestion_map:
        user_input = suggestion_map[trigger_id]
    else:
        user_input = input_value
    
    if not user_input:
        return [no_update] * 8
    
    # Create user message
    user_message = html.Div([
        html.Div([
            html.Div("U", className="user-avatar"),
            html.Span("You", className="model-name")
        ], className="user-info"),
        html.Div(user_input, className="message-text")
    ], className="user-message message")
    
    # Add user message to chat
    updated_messages = current_messages + [user_message] if current_messages else [user_message]
    
    # Add thinking indicator
    thinking_indicator = html.Div([
        html.Div([
            html.Span(className="spinner"),
            html.Span("Thinking...")
        ], className="thinking-indicator")
    ], className="bot-message message")
    
    updated_messages.append(thinking_indicator)
    
    # Handle session management
    if session_data["current_session"] is None:
        session_data = {"current_session": len(chat_history) if chat_history else 0}
    
    current_session = session_data["current_session"]
    
    # Update chat history
    if chat_history is None:
        chat_history = []
    
    if current_session < len(chat_history):
        chat_history[current_session]["messages"] = updated_messages
        chat_history[current_session]["queries"].append(user_input)
    else:
        chat_history.insert(0, {
            "session_id": current_session,
            "queries": [user_input],
            "messages": updated_messages
        })
    
    # Update chat list
    updated_chat_list = []
    for i, session in enumerate(chat_history):
        first_query = session["queries"][0]
        is_active = (i == current_session)
        updated_chat_list.append(
            html.Div(
                first_query,
                className=f"chat-item {'active' if is_active else ''}",
                id={"type": "chat-item", "index": i}
            )
        )
    
    return (updated_messages, "", "welcome-container hidden",
            {"trigger": True, "message": user_input}, True,
            updated_chat_list, chat_history, session_data)


# Second callback: Make API call and show response
@app.callback(
    [Output("chat-messages", "children", allow_duplicate=True),
     Output("chat-history-store", "data", allow_duplicate=True),
     Output("chat-trigger", "data", allow_duplicate=True),
     Output("query-running-store", "data", allow_duplicate=True)],
    [Input("chat-trigger", "data")],
    [State("chat-messages", "children"),
     State("chat-history-store", "data"),
     State("session-store", "data")],
    prevent_initial_call=True
)
def get_model_response(trigger_data, current_messages, chat_history, session_data):
    if not trigger_data or not trigger_data.get("trigger"):
        return no_update, no_update, no_update, no_update
    
    user_input = trigger_data.get("message", "")
    if not user_input:
        return no_update, no_update, no_update, no_update
    
    try:
        # Track start time for telemetry
        start_time = time.time()
        
        # Convert chat history to input messages
        input_messages = [{"role": "user", "content": user_input}]
        
        # Query endpoint
        messages, request_id = query_endpoint(
            endpoint_name=SERVING_ENDPOINT,
            messages=input_messages,
            return_traces=ENDPOINT_SUPPORTS_FEEDBACK
        )
        
        # Calculate response time
        response_time_ms = int((time.time() - start_time) * 1000)
        
        # Extract response content
        response_text = ""
        for msg in messages:
            if isinstance(msg, dict) and msg.get("content"):
                response_text += msg.get("content", "")
        logger.info("Response time: %s ms", response_time_ms)
        logger.info("User input: %s", user_input[:10000])
        logger.info("Response text: %s", response_text[:10000])
        logger.info("Request ID: %s", request_id)
        logger.info("Messages: %s", messages)
        logger.info("Session data: %s", session_data)
        logger.info("Chat history: %s", chat_history)
        logger.info("Current messages: %s", current_messages)
        # Send telemetry to Zerobus with automatic retry
        try:
            record = record_pb2.Chat(
                telemetry_id=str(uuid.uuid4()),
                user_message=user_input[:10000],  # Limit size
                assistant_message=response_text[:10000],
                response_time_ms=response_time_ms
            )
            send_telemetry_with_retry(record, max_retries=3)
        except Exception as e:
            logger.warning(f"Unexpected error sending telemetry: {str(e)}")
        
        # Create bot response
        bot_response = html.Div([
            html.Div([
                html.Div(className="model-avatar"),
                html.Span("Assistant", className="model-name")
            ], className="model-info"),
            html.Div([
                dcc.Markdown(response_text, className="message-text"),
                html.Div([
                    html.Div([
                        html.Button(
                            id={"type": "thumbs-up-button", "index": len(chat_history)},
                            className="thumbs-up-button"
                        ),
                        html.Button(
                            id={"type": "thumbs-down-button", "index": len(chat_history)},
                            className="thumbs-down-button"
                        )
                    ], className="message-actions")
                ], className="message-footer")
            ], className="message-content")
        ], className="bot-message message")
        
        # Update messages
        if chat_history and len(chat_history) > 0:
            chat_history[0]["messages"] = current_messages[:-1] + [bot_response]
        
        return current_messages[:-1] + [bot_response], chat_history, {"trigger": False, "message": ""}, False
        
    except Exception as e:
        logger.error(f"Error getting response: {str(e)}")
        error_msg = f"Sorry, I encountered an error: {str(e)}"
        error_response = html.Div([
            html.Div([
                html.Div(className="model-avatar"),
                html.Span("Assistant", className="model-name")
            ], className="model-info"),
            html.Div([
                html.Div(error_msg, className="message-text")
            ], className="message-content")
        ], className="bot-message message")
        
        if chat_history and len(chat_history) > 0:
            chat_history[0]["messages"] = current_messages[:-1] + [error_response]
        
        return current_messages[:-1] + [error_response], chat_history, {"trigger": False, "message": ""}, False


# Toggle sidebar
@app.callback(
    [Output("sidebar", "className"),
     Output("new-chat-button", "style"),
     Output("sidebar-new-chat-button", "style"),
     Output("logo-container", "className"),
     Output("nav-left", "className"),
     Output("left-component", "className"),
     Output("main-content", "className")],
    [Input("sidebar-toggle", "n_clicks")],
    [State("sidebar", "className"),
     State("left-component", "className"),
     State("main-content", "className")]
)
def toggle_sidebar(n_clicks, current_sidebar_class, current_left_component_class, current_main_content_class):
    if n_clicks:
        if "sidebar-open" in current_sidebar_class:
            return "sidebar", {"display": "flex"}, {"display": "none"}, "logo-container", "nav-left", "left-component", "main-content"
        else:
            return "sidebar sidebar-open", {"display": "none"}, {"display": "flex"}, "logo-container logo-container-open", "nav-left nav-left-open", "left-component left-component-open", "main-content main-content-shifted"
    return current_sidebar_class, {"display": "flex"}, {"display": "none"}, "logo-container", "nav-left", "left-component", current_main_content_class


# Chat item selection
@app.callback(
    [Output("chat-messages", "children", allow_duplicate=True),
     Output("welcome-container", "className", allow_duplicate=True),
     Output("chat-list", "children", allow_duplicate=True),
     Output("session-store", "data", allow_duplicate=True)],
    [Input({"type": "chat-item", "index": ALL}, "n_clicks")],
    [State("chat-history-store", "data"),
     State("chat-list", "children"),
     State("session-store", "data")],
    prevent_initial_call=True
)
def show_chat_history(n_clicks, chat_history, current_chat_list, session_data):
    triggered_ctx = ctx
    if not triggered_ctx.triggered:
        return no_update, no_update, no_update, no_update
    
    triggered_id = triggered_ctx.triggered[0]["prop_id"].split(".")[0]
    clicked_index = json.loads(triggered_id)["index"]
    
    if not chat_history or clicked_index >= len(chat_history):
        return no_update, no_update, no_update, no_update
    
    new_session_data = {"current_session": clicked_index}
    
    # Update active state in chat list
    updated_chat_list = []
    for i, item in enumerate(current_chat_list):
        new_class = "chat-item active" if i == clicked_index else "chat-item"
        updated_chat_list.append(
            html.Div(
                item["props"]["children"],
                className=new_class,
                id={"type": "chat-item", "index": i}
            )
        )
    
    return (chat_history[clicked_index]["messages"], 
            "welcome-container hidden", 
            updated_chat_list,
            new_session_data)


# Auto-scroll
app.clientside_callback(
    """
    function(children) {
        var chatMessages = document.getElementById('chat-messages');
        if (chatMessages) {
            chatMessages.scrollTop = chatMessages.scrollHeight;
        }
        return '';
    }
    """,
    Output('dummy-output', 'children'),
    Input('chat-messages', 'children'),
    prevent_initial_call=True
)


# New chat button
@app.callback(
    [Output("welcome-container", "className", allow_duplicate=True),
     Output("chat-messages", "children", allow_duplicate=True),
     Output("chat-trigger", "data", allow_duplicate=True),
     Output("query-running-store", "data", allow_duplicate=True),
     Output("chat-history-store", "data", allow_duplicate=True),
     Output("session-store", "data", allow_duplicate=True)],
    [Input("new-chat-button", "n_clicks"),
     Input("sidebar-new-chat-button", "n_clicks")],
    [State("chat-history-store", "data")],
    prevent_initial_call=True
)
def reset_to_welcome(n_clicks1, n_clicks2, chat_history_store):
    new_session_data = {"current_session": None}
    return ("welcome-container visible", [], {"trigger": False, "message": ""}, 
            False, chat_history_store, new_session_data)


# Disable input while query is running
@app.callback(
    [Output("chat-input-fixed", "disabled"),
     Output("send-button-fixed", "disabled"),
     Output("new-chat-button", "disabled"),
     Output("sidebar-new-chat-button", "disabled"),
     Output("query-tooltip", "className")],
    [Input("query-running-store", "data")],
    prevent_initial_call=True
)
def toggle_input_disabled(query_running):
    tooltip_class = "query-tooltip visible" if query_running else "query-tooltip hidden"
    return query_running, query_running, query_running, query_running, tooltip_class


# Thumbs up/down feedback
@app.callback(
    [Output({"type": "thumbs-up-button", "index": MATCH}, "className"),
     Output({"type": "thumbs-down-button", "index": MATCH}, "className")],
    [Input({"type": "thumbs-up-button", "index": MATCH}, "n_clicks"),
     Input({"type": "thumbs-down-button", "index": MATCH}, "n_clicks")],
    [State({"type": "thumbs-up-button", "index": MATCH}, "className"),
     State({"type": "thumbs-down-button", "index": MATCH}, "className")],
    prevent_initial_call=True
)
def handle_feedback(up_clicks, down_clicks, up_class, down_class):
    triggered_ctx = ctx
    if not triggered_ctx.triggered:
        return no_update, no_update
    
    trigger_id = triggered_ctx.triggered[0]["prop_id"].split(".")[0]
    button_type = json.loads(trigger_id)["type"]
    
    if button_type == "thumbs-up-button":
        new_up_class = "thumbs-up-button active" if "active" not in up_class else "thumbs-up-button"
        new_down_class = "thumbs-down-button"
    else:
        new_up_class = "thumbs-up-button"
        new_down_class = "thumbs-down-button active" if "active" not in down_class else "thumbs-down-button"
    
    return new_up_class, new_down_class


if __name__ == "__main__":
    app.run(
        debug=True,
        host="0.0.0.0",
        port=8000
    )

