from flask import Flask, send_from_directory

app = Flask(__name__, static_folder=".")

@app.route("/")
def index():
    return send_from_directory(".", "kp_stocks.html")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)