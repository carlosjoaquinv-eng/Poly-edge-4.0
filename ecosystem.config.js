module.exports = {
  apps: [{
    name: 'polyedge-v4',
    script: 'main_v4.py',
    interpreter: '/usr/bin/python3',
    cwd: '/root/polyedge/v4',
    max_restarts: 10,
    min_uptime: '30s',
    restart_delay: 5000,
    exp_backoff_restart_delay: 1000,
    max_memory_restart: '500M',
    error_file: '/root/polyedge/v4/logs/v4-error.log',
    out_file: '/root/polyedge/v4/logs/v4-out.log',
    log_file: '/root/polyedge/v4/logs/polyedge_v4.log',
    merge_logs: true,
    log_date_format: 'YYYY-MM-DD HH:mm:ss',
    env: { PYTHONUNBUFFERED: '1', PAPER_MODE: 'true' },
    kill_timeout: 10000,
    watch: false,
    ignore_watch: ['logs', 'data', '__pycache__', '*.pyc', 'backups']
  }]
};