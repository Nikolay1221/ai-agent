import os
import json
import subprocess
import threading
import time
import queue
import requests
import re
import logging

def estimate_tokens(text: str) -> int:
    """
    Простая оценка количества токенов в тексте.
    Примерно 1 токен = 4 символа для английского текста,
    для русского текста примерно 1 токен = 3 символа.
    """
    # Простая эвристика: считаем слова и символы
    words = len(text.split())
    chars = len(text)
    
    # Примерная оценка: 1 токен ≈ 4 символа для смешанного текста
    estimated_tokens = chars // 4
    
    # Минимальная оценка: количество слов
    min_tokens = words
    
    return max(estimated_tokens, min_tokens)

# Setup logging to file with explicit UTF-8 encoding
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(message)s',
                    filename='agent.log',
                    filemode='w',
                    encoding='utf-8')

# Also create a logger to print to console for debugging Flask
console_logger = logging.getLogger('console_logger')
console_logger.addHandler(logging.StreamHandler())
console_logger.setLevel(logging.INFO)

class Agent:
    def __init__(self, mcp_server_command: list[str]):
        self.request_id = 1
        self.mcp_process = self._start_mcp_process(mcp_server_command)
        
        self.stdout_queue = queue.Queue()
        self.stderr_queue = queue.Queue()
        
        threading.Thread(target=self._enqueue_output, args=(self.mcp_process.stdout, self.stdout_queue), daemon=True).start()
        threading.Thread(target=self._enqueue_output, args=(self.mcp_process.stderr, self.stderr_queue), daemon=True).start()
        threading.Thread(target=self._log_stderr, daemon=True).start()
        
        tool_names = self._perform_handshake()
        
        self.tools_schema = self._discover_tool_schemas(tool_names)
        # No schema validation needed for minimal setup

        # Local Gemma3 4B configuration
        self.mixtral_url = "http://localhost:11434/api/generate"
        self.mixtral_model = "gemma3:4b"
        
        logging.info("\n✅ Agent initialized with minimal user info tools.")
        logging.info(f"- Using local Gemma3 4B model: {self.mixtral_model}")

    def _start_mcp_process(self, command):
        try:
            return subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding='utf-8', bufsize=1
            )
        except FileNotFoundError:
            raise RuntimeError(f"Could not find MCP server command: '{' '.join(command)}'.")

    def _enqueue_output(self, pipe, q):
        for line in iter(pipe.readline, ''):
            q.put(line)
        pipe.close()

    def _log_stderr(self):
        while True:
            try:
                line = self.stderr_queue.get_nowait()
                if line:
                    logging.info(f"[MCP stderr] {line.strip()}")
            except queue.Empty:
                time.sleep(0.1)

    def _send_mcp_raw(self, payload: dict):
        if self.mcp_process.stdin and not self.mcp_process.stdin.closed:
            line_to_send = json.dumps(payload) + '\n'
            # print(f"[MCP RAW stdin] {line_to_send.strip()}") # Silencing for cleaner output
            self.mcp_process.stdin.write(line_to_send)
            self.mcp_process.stdin.flush()

    def _perform_handshake(self) -> list[str]:
        logging.info("🤝 Performing MCP handshake...")
        
        init_id = self.request_id
        self.request_id += 1
        self._send_mcp_raw({
            "jsonrpc": "2.0", 
            "id": init_id, 
            "method": "initialize", 
            "params": {
                "protocolVersion": "2024-11-05", 
                "capabilities": {}, 
                "clientInfo": {"name": "agent-client", "version": "1.0.0"}
            }
        })
        
        got_init_response = False
        tool_names = []
        
        start_time = time.time()
        while time.time() - start_time < 3600:
            try:
                line = self.stdout_queue.get(timeout=0.1)
                # print(f"[MCP RAW stdout] {line.strip()}")
                msg = json.loads(line)
                
                if msg.get('id') == init_id:
                    logging.info("✅ Received initialize response")
                    got_init_response = True
                elif msg.get('method') == 'tools_ready':
                    logging.info("✅ Received 'tools_ready' notification - SERVER IS READY!")
                    tool_names = msg.get('params', {}).get('tools', [])
                    
            except (queue.Empty, json.JSONDecodeError):
                continue
                
            if got_init_response and tool_names:
                break
        else:
            self.shutdown()
            raise RuntimeError("Handshake timed out")

        self._send_mcp_raw({"jsonrpc": "2.0", "method": "notifications/initialized"})
        logging.info("🤝 Handshake complete!")
        return tool_names

    def _discover_tool_schemas(self, tool_names: list[str]) -> list[dict]:
        logging.info("\n🔍 Using minimal tool configuration...")
        # Return empty list - no JSON schema needed
        return []



    def _wait_for_response(self, request_id: int, timeout: int = 3600) -> dict:
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                line = self.stdout_queue.get(timeout=0.1)
                # print(f"[MCP RAW stdout] {line.strip()}")
                response = json.loads(line)
                if response.get('id') == request_id:
                    if 'error' in response:
                        logging.error(f"MCP tool call error: {response['error']}")
                        return {}
                    return response.get('result', {})
            except (queue.Empty, json.JSONDecodeError):
                continue
        logging.warning(f"Timeout waiting for response to request ID {request_id}")
        return {}
    
    def call_mcp_tool(self, name: str, arguments: dict) -> dict:
        current_id = self.request_id
        self.request_id += 1
        
        payload = {"jsonrpc": "2.0", "id": current_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
        self._send_mcp_raw(payload)
        
        return self._wait_for_response(current_id)

    def _create_reasoning_prompt(self, task: str, history: list) -> str:
        # Limit context to 5000 tokens by trimming history
        # Account for system prompt (~20 tokens)
        system_prompt = "You are a VK messaging assistant. You can search users, get dialogs, get message history, and chat with people. Always respond with valid JSON."
        system_tokens = estimate_tokens(system_prompt)
        max_context_tokens = 5000 - system_tokens
        
        # Get tool hints once
        tools_hints = self._load_tools_hints()
        
        # Calculate how much history we can include
        base_prompt_template = f"""VK messaging assistant. Task: "{task}"

Tools: {tools_hints}

History: {{HISTORY_PLACEHOLDER}}

ПРОСТОЙ АЛГОРИТМ ПЕРЕПИСКИ:
1. Получить непрочитанные сообщения: messages.get_unread_messages
2. Ответить на каждое сообщение: messages.send_message (peer_id, "Привет! Как дела?")
3. Ждать новые сообщения: messages.await_any_event (duration_s=60)

ВАЖНО: 
- Отвечай на каждое непрочитанное сообщение
- Пиши только на русском языке
- Будь дружелюбным и общительным

ПРИМЕРЫ:
Получить сообщения: `{{"tool": "messages", "arguments": {{"method": "get_unread_messages", "params": {{}}}}}}`
Ответить: `{{"tool": "messages", "arguments": {{"method": "send_message", "params": {{"peer_id": "821539637", "message": "Привет! Как дела?"}}}}}}`
Ждать: `{{"tool": "messages", "arguments": {{"method": "await_any_event", "params": {{"duration_s": 60}}}}}}`

JSON response:"""
        
        # Start with empty history and add items until we reach limit
        limited_history = []
        current_tokens = estimate_tokens(base_prompt_template.replace("{HISTORY_PLACEHOLDER}", "[]"))
        
        for item in reversed(history):  # Start from most recent
            item_str = json.dumps(item, ensure_ascii=False)
            item_tokens = estimate_tokens(item_str)
            
            # Test if adding this item would exceed limit
            test_history = limited_history.copy()
            test_history.insert(0, item)
            test_history_str = json.dumps(test_history, indent=2, ensure_ascii=False)
            test_prompt = base_prompt_template.replace("{HISTORY_PLACEHOLDER}", test_history_str)
            test_tokens = estimate_tokens(test_prompt)
            
            if test_tokens <= max_context_tokens:
                limited_history.insert(0, item)  # Add to beginning to maintain order
                current_tokens = test_tokens
            else:
                break
        
        # Build final prompt
        history_json_str = json.dumps(limited_history, indent=2, ensure_ascii=False)
        prompt = base_prompt_template.replace("{HISTORY_PLACEHOLDER}", history_json_str)

        # Подсчёт токенов в промпте
        prompt_tokens = estimate_tokens(prompt)
        
        logging.info(f"Context window: {max_context_tokens} tokens max")
        logging.info(f"Final prompt tokens: {prompt_tokens}")
        logging.info(f"History items included: {len(limited_history)}")
        
        # Проверка лимита и принудительное ограничение
        if prompt_tokens > max_context_tokens:
            logging.warning(f"⚠️  WARNING: Prompt exceeds limit! {prompt_tokens} > {max_context_tokens}")
            # Force reset if we exceed limit
            needs_reset = True
            items_to_keep = 0  # Start fresh
            # Create minimal prompt without history
            minimal_prompt = f"""VK messaging assistant. Task: "{task}"

Tools: {tools_hints}

History: []

ПРОСТОЙ АЛГОРИТМ ПЕРЕПИСКИ:
1. Получить непрочитанные сообщения: messages.get_unread_messages
2. Ответить на каждое сообщение: messages.send_message (peer_id, "Привет! Как дела?")
3. Ждать новые сообщения: messages.await_any_event (duration_s=60)

ВАЖНО: 
- Отвечай на каждое непрочитанное сообщение
- Пиши только на русском языке
- Будь дружелюбным и общительным

ПРИМЕРЫ (ТОЛЬКО JSON БЕЗ ОБЕРТКИ):
Получить сообщения: `{{"tool": "messages", "arguments": {{"method": "get_unread_messages", "params": {{}}}}}}`
Ответить: `{{"tool": "messages", "arguments": {{"method": "send_message", "params": {{"peer_id": "821539637", "message": "Привет! Как дела?"}}}}}}`
Ждать: `{{"tool": "messages", "arguments": {{"method": "await_any_event", "params": {{"duration_s": 60}}}}}}`

ВАЖНО: НЕ ОБОРАЧИВАЙ В {"action": {...}}! ПИШИ ТОЛЬКО JSON!

JSON response:"""
            prompt = minimal_prompt
            logging.info(f"🔄 Forced history reset - using minimal prompt")
        else:
            logging.info(f"✅ Context within limit: {prompt_tokens} <= {max_context_tokens}")
            needs_reset = len(limited_history) < len(history) and len(history) > 0
            items_to_keep = len(limited_history) if needs_reset else len(history)
        
        return prompt, needs_reset, items_to_keep

    def _load_tools_hints(self) -> str:
        """Return tool hints for messaging only"""
        return """Available tools:
1. "messages" tool with method "get_unread_messages" - получить непрочитанные сообщения
2. "messages" tool with method "get_conversations" - получить список диалогов
3. "messages" tool with method "get_history" - получить историю сообщений (peer_id, count=10)
4. "messages" tool with method "send_message" - отправить сообщение (peer_id, message)
5. "messages" tool with method "await_any_event" - ждать новые сообщения (duration_s=60)

Алгоритм общения: получить непрочитанные → получить диалоги → написать всем."""

    def run(self):
        try:
            with open('goal.txt', 'r', encoding='utf-8') as f:
                task = f.read().strip()
                if not task:
                    logging.info("Goal file is empty. Agent has nothing to do.")
                    return
        except FileNotFoundError:
            logging.info("goal.txt not found. Agent has nothing to do.")
            return

        logging.info(f"\n--- Starting Agent Run with task: '{task}' ---")
        
        # Load history or start fresh
        history_file = 'history.json'
        history = []
        if os.path.exists(history_file):
            try:
                with open(history_file, 'r', encoding='utf-8') as f:
                    history = json.load(f)
                    logging.info(f"Loaded {len(history)} previous steps from history.json")
            except (json.JSONDecodeError, IOError):
                logging.info("Could not load history.json, starting fresh.")
        
        step = len(history) + 1
        
        # REMOVED msvcrt check. Correction will be handled by file.
        
        while True: # The loop stays for multi-step tasks, but the interruption logic changes
            # Check for pause state
            if os.path.exists('paused.flag'):
                logging.info("Agent is paused. Waiting...")
                
                # Wait until the pause flag is removed
                while os.path.exists('paused.flag'):
                    time.sleep(1)
                
                logging.info("Agent resumed by user. Re-evaluating goal...")
                # Re-read the goal file in case it was changed
                try:
                    with open('goal.txt', 'r', encoding='utf-8') as f:
                        new_task = f.read().strip()
                        if new_task != task:
                            logging.info(f"Goal has been updated to: '{new_task}'")
                            task = new_task
                            history.append({"action": "goal_updated", "result": {"new_goal": task}})
                except (IOError, FileNotFoundError):
                    logging.warning("Could not re-read goal file after pause. Continuing with old goal.")

            logging.info(f"\n- Step {step} -")
            
            # 1. Reason
            prompt, needs_reset, items_to_keep = self._create_reasoning_prompt(task, history)
            
            # Check if we need to reset history due to token limit
            if needs_reset:
                logging.info(f"🔄 History reset needed - keeping only {items_to_keep} recent items due to token limit")
                # Keep only the most recent items that fit in context
                history = history[-items_to_keep:] if items_to_keep > 0 else []
                step = len(history) + 1
                logging.info(f"History reset: now has {len(history)} items")
            
            response_text = self.ask_mixtral(prompt)
            logging.info(f"Gemma3 4B response: {response_text}")
            
            # Подсчёт общих токенов в контексте
            total_context_tokens = estimate_tokens(prompt) + estimate_tokens(response_text)
            logging.info(f"Total context tokens (prompt + response): {total_context_tokens}")

            # 2. Parse action
            try:
                json_match = re.search(r'```json\s*(\{.*?\})\s*```', response_text, re.DOTALL)
                if not json_match:
                    json_match = re.search(r'(\{.*?\})', response_text, re.DOTALL)
                
                if not json_match:
                    logging.warning("Could not parse JSON from Gemma3 4B response. Adding error to history.")
                    history.append({"action": "reasoning_error", "raw_response": response_text, "error": "No JSON object found"})
                    continue

                action_str = json_match.group(1)
                action = json.loads(action_str)

                # Handle wrapped action format: {"action": {...}}
                if "action" in action and isinstance(action["action"], dict):
                    action = action["action"]

                if "final_answer" in action:
                    logging.info(f"\n--- AGENT PROPOSES FINAL ANSWER ---")
                    logging.info(f"Proposed Answer: {action['final_answer']}")
                    
                    # REMOVED msvcrt check. Correction will be handled by file.
                    
                    if action['final_answer'] == "Your detailed answer here.": # Assuming this is the expected final answer
                        logging.info("\n--- GOAL ACHIEVED ---")
                        break 
                    else:
                        detailed_feedback = input("Understood. What was wrong with the proposed answer? ")
                        logging.info("Feedback received. Continuing task...")
                        history.append({
                            "action": {"final_answer_proposed": action['final_answer']},
                            "result": {"user_feedback": detailed_feedback or "User rejected the answer."}
                        })
                        step += 1
                        continue

                tool_name = action.get('tool')
                tool_args = action.get('arguments', {})
                
                if not tool_name or not isinstance(tool_args, dict):
                    raise ValueError("Invalid tool call structure in Gemma3 4B response")

            except (json.JSONDecodeError, ValueError) as e:
                logging.error(f"Error parsing Gemma3 4B's action: {e}. Adding error to history.")
                history.append({"action": "parsing_error", "raw_response": response_text, "error": str(e)})
                continue

            # 3. Execute action
            logging.info(f"Executing tool '{tool_name}' with args: {tool_args}")
            result = self.call_mcp_tool(tool_name, tool_args)
            
            logging.info(f"Tool Result: {json.dumps(result, indent=2, ensure_ascii=False)}")
            
            # Learn from successful discoveries
            if (tool_args.get("method") == "__capabilities__" and
                result and not result.get("isError") and
                "content" in result and isinstance(result["content"], list)):

                capabilities = [item.get("text") for item in result["content"] if "text" in item]
                if capabilities:
                    logging.info(f"🧠 Learned new capabilities for tool '{tool_name}'.")
                    if tool_name not in self.knowledge:
                        self.knowledge[tool_name] = {}
                    self.knowledge[tool_name]["capabilities"] = capabilities
                    self._save_knowledge()

            # 4. Record result and save history
            history.append({"action": {"tool": tool_name, "arguments": tool_args}, "result": result})
            try:
                with open(history_file, 'w', encoding='utf-8') as f:
                    json.dump(history, f, indent=2, ensure_ascii=False)
            except IOError as e:
                logging.warning(f"Warning: Could not save history to {history_file}: {e}")
            
            # Check for emergency correction via file (this part stays)
            correction_file = 'correction.txt'
            if os.path.exists(correction_file):
                try:
                    with open(correction_file, 'r', encoding='utf-8') as f:
                        correction_text = f.read().strip()
                    if correction_text:
                        logging.info(f"\n🚨 EMERGENCY CORRECTION RECEIVED: '{correction_text}'")
                        history.append({"action": "emergency_correction", "result": {"user_feedback": correction_text}})
                    os.remove(correction_file)
                except (IOError, OSError) as e:
                    logging.warning(f"Warning: Could not process correction file: {e}")

            step += 1

    def shutdown(self):
        logging.info("\nShutting down...")
        if self.mcp_process:
            self.mcp_process.terminate()
            try:
                self.mcp_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.mcp_process.kill()
        logging.info("Shutdown complete.")



    def ask_mixtral(self, prompt: str, retries: int = 3, delay: int = 2) -> str:
        logging.info("Asking local Gemma3 4B for next action...")
        
        data = {
            "model": self.mixtral_model,
            "prompt": prompt,
            "stream": False  # We want a complete response, not streaming
        }
        
        for attempt in range(retries):
            try:
                response = requests.post(self.mixtral_url, json=data, timeout=60)
                response.raise_for_status()
                
                result = response.json()
                if 'response' in result:
                    response_text = result['response']
                    response_tokens = estimate_tokens(response_text)
                    logging.info(f"Model response tokens: {response_tokens}")
                    return response_text
                else:
                    logging.warning(f"Unexpected Gemma3 4B response format: {result}")
                    return ""
                    
            except requests.exceptions.RequestException as e:
                logging.warning(f"Local Gemma3 4B API call error (attempt {attempt + 1}/{retries}): {e}. Retrying in {delay}s...")
                time.sleep(delay)
            except Exception as e:
                logging.error(f"An unexpected error occurred with local Gemma3 4B API: {e}")
                return ""
        
        logging.error(f"Local Gemma3 4B API call failed after {retries} retries.")
        return ""


if __name__ == "__main__":
    try:
        agent = Agent(["mcp", "run", "C:\\Users\\nikol\\Projects\\mcp_vk_api\\main.py"])
        agent.run()
    except Exception as e:
        logging.error(f"\n❌ Agent failed with error: {e}")
        console_logger.error(f"\n❌ Agent failed with error: {e}")
    finally:
        if 'agent' in locals():
            agent.shutdown()
