# Remote Access from Laptop

## 1. Enable SSH on this PC (run once, requires sudo)
```bash
sudo systemctl enable --now sshd
```

## 2. Connect from laptop (basic)
```bash
ssh mike@192.168.4.54
```

## 3. Connect with dashboard port forwarding
Gives you http://localhost:8100 on your laptop:
```bash
# Note: use 192.168.4.54 not 127.0.0.1 — pasta networking binds to host IP, not loopback
ssh -L 8100:192.168.4.54:8100 -L 8103:192.168.4.54:8103 mike@192.168.4.54
```

## 4. Laptop ~/.ssh/config (paste on your laptop)
```
Host ringcatch
    HostName 192.168.4.54
    User mike
    LocalForward 8100 192.168.4.54:8100
    LocalForward 8103 192.168.4.54:8103
    LocalForward 8104 192.168.4.54:8104
    LocalForward 8107 192.168.4.54:8107
    ServerAliveInterval 60
    ServerAliveCountMax 3
```
Then just: `ssh ringcatch`

## 5. tmux — available after next reboot
Once tmux is installed (after reboot), start a persistent session:
```bash
tmux new -s agency      # create session
tmux attach -t agency   # re-attach from anywhere
```

Useful tmux keys:
- Ctrl+b d   → detach (session keeps running)
- Ctrl+b c   → new window
- Ctrl+b n   → next window
- Ctrl+b %   → split vertical
- Ctrl+b "   → split horizontal

## 6. Check all agents from laptop (after SSH tunnel)
```bash
for port in 8100 8101 8102 8103 8104 8105 8106 8107; do
  echo "Port $port: $(curl -s http://localhost:$port/health)"
done
```

## 7. Dashboard URL (after SSH tunnel)
http://localhost:8100
