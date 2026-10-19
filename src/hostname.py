import socket

def get_hostname():
    hostname = socket.gethostname()
    return hostname
    
print(get_hostname())