from flask import Flask, jsonify
from flask_cors import CORS
import subprocess
import os

app = Flask(__name__)
# 允许来自前端域名的跨域请求
CORS(app, resources={r"/*": {"origins": "*"}})

bot_process = None

@app.route('/api/bot/start', methods=['POST'])
def start_bot():
    global bot_process
    if bot_process is not None and bot_process.poll() is None:
        return jsonify({"status": "already_running"}), 200
    
    # 启动 bot (使用 run 模式)
    try:
        import sys
        
        # 增加 PYTHONUNBUFFERED=1 确保实时输出日志
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        
        bot_process = subprocess.Popen(
            ["python3", "trade_v2.py", "run"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            stdout=sys.stdout,
            stderr=sys.stderr,
            env=env
        )
        return jsonify({"status": "started", "pid": bot_process.pid}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/api/bot/stop', methods=['POST'])
def stop_bot():
    global bot_process
    if bot_process is None or bot_process.poll() is not None:
        return jsonify({"status": "already_stopped"}), 200
    
    try:
        bot_process.terminate()
        bot_process.wait(timeout=5)
        bot_process = None
        return jsonify({"status": "stopped"}), 200
    except Exception as e:
        # 强制结束
        if bot_process:
            bot_process.kill()
            bot_process = None
        return jsonify({"status": "killed", "message": str(e)}), 200

@app.route('/api/bot/status', methods=['GET'])
def get_bot_status():
    global bot_process
    if bot_process is not None and bot_process.poll() is None:
        return jsonify({"status": "running", "pid": bot_process.pid}), 200
    return jsonify({"status": "stopped"}), 200

from flask import send_from_directory

@app.route('/data/<filename>', methods=['GET'])
def get_data(filename):
    if filename in ['status.json', 'thinking.json', 'trades.json', 'strategy_v2.json']:
        return send_from_directory(os.path.dirname(os.path.abspath(__file__)), filename)
    return jsonify({"error": "File not found"}), 404

if __name__ == '__main__':
    print("正在启动 AI 交易员本地控制台...")
    print("控制接口: http://127.0.0.1:5000")
    app.run(host='127.0.0.1', port=5000)
