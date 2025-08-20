from flask import Flask, render_template, request, jsonify, send_file
import subprocess
import os
import sys
import time
import psutil

app = Flask(__name__)

PID_FILE = 'agent.pid'

def is_agent_running():
    """Check if the agent process is currently running."""
    if not os.path.exists(PID_FILE):
        return False
    try:
        with open(PID_FILE, 'r') as f:
            pid = int(f.read().strip())
        # Check if a process with this PID exists and has the correct name
        process = psutil.Process(pid)
        # This check is an approximation; on Windows, name() might be 'python.exe'
        return "python" in process.name().lower()
    except (IOError, ValueError, psutil.NoSuchProcess, psutil.AccessDenied):
        # PID file is stale or unreadable, or process is gone
        return False

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/get_tools')
def get_tools():
    try:
        return send_file('static/tools.json', mimetype='application/json')
    except FileNotFoundError:
        return jsonify({"error": "tools.json not found!"}), 404

@app.route('/status')
def get_status():
    """Endpoint for the frontend to check if the agent is running."""
    return jsonify({"is_running": is_agent_running()})
    
@app.route('/start', methods=['POST'])
def start_agent():
    if is_agent_running():
        return jsonify({"status": "error", "message": "Agent is already running."}), 400

    goal = request.json.get('goal', '')
    if not goal:
        return jsonify({"status": "error", "message": "Goal cannot be empty."}), 400

    try:
        # Clear log and other temp files, but not history or knowledge
        if os.path.exists('agent.log'): os.remove('agent.log')
        with open('goal.txt', 'w', encoding='utf-8') as f: f.write(goal)
        if os.path.exists(PID_FILE): os.remove(PID_FILE)
    except IOError as e:
        return jsonify({"status": "error", "message": f"Failed to prepare environment: {e}"}), 500

    python_executable = sys.executable
    # Start the agent as a completely detached process
    process = subprocess.Popen([python_executable, "agent.py"])
    
    try:
        # Save the new PID
        with open(PID_FILE, 'w') as f:
            f.write(str(process.pid))
        print(f"Agent process started with PID: {process.pid}")
        return jsonify({"status": "success", "message": "Agent started."})
    except IOError as e:
        return jsonify({"status": "error", "message": f"Failed to write PID file: {e}"}), 500

@app.route('/stop', methods=['POST'])
def stop_agent():
    if not is_agent_running():
        return jsonify({"status": "error", "message": "Agent is not running."}), 400
        
    try:
        with open(PID_FILE, 'r') as f:
            pid = int(f.read().strip())
        process = psutil.Process(pid)
        process.terminate() # or process.kill()
        process.wait(timeout=5)
    except (IOError, ValueError, psutil.NoSuchProcess, psutil.TimeoutExpired) as e:
        print(f"Could not stop process {pid}: {e}")
    finally:
        # Clean up the PID file
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
            
    print("Agent process stopped.")
    return jsonify({"status": "success", "message": "Agent stopped."})

@app.route('/pause', methods=['POST'])
def pause_agent():
    pause_flag_file = 'paused.flag'
    should_pause = request.json.get('pause', True)

    if should_pause:
        if not os.path.exists(pause_flag_file):
            try:
                open(pause_flag_file, 'w').close()
                return jsonify({"status": "success", "message": "Agent paused."})
            except IOError as e:
                return jsonify({"status": "error", "message": f"Failed to create pause flag: {e}"}), 500
    else:
        if os.path.exists(pause_flag_file):
            try:
                os.remove(pause_flag_file)
                return jsonify({"status": "success", "message": "Agent resumed."})
            except IOError as e:
                return jsonify({"status": "error", "message": f"Failed to remove pause flag: {e}"}), 500
                
    return jsonify({"status": "no_change"})

@app.route('/update_goal', methods=['POST'])
def update_goal():
    new_goal = request.json.get('goal', '')
    if not new_goal:
        return jsonify({"status": "error", "message": "Goal cannot be empty."}), 400
    try:
        with open('goal.txt', 'w', encoding='utf-8') as f:
            f.write(new_goal)
        return jsonify({"status": "success", "message": "Goal updated."})
    except IOError as e:
        return jsonify({"status": "error", "message": f"Failed to write goal file: {e}"}), 500

@app.route('/log')
def log_stream():
    # This route is kept for polling, but streaming is preferred
    if os.path.exists('agent.log'):
        return send_file('agent.log', mimetype='text/plain')
    return ""

@app.route('/submit_correction', methods=['POST'])
def submit_correction():
    correction = request.json.get('correction', '')
    if not correction:
        return jsonify({"status": "error", "message": "Correction cannot be empty."}), 400
    
    try:
        with open('correction.txt', 'w', encoding='utf-8') as f:
            f.write(correction)
        return jsonify({"status": "success", "message": "Correction submitted."})
    except IOError as e:
        return jsonify({"status": "error", "message": f"Failed to write correction file: {e}"}), 500

if __name__ == '__main__':
    # Disable reloader for stability
    app.run(debug=True, port=5001, use_reloader=False)
