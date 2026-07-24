from urllib.parse import urlparse
import paramiko

# Tu URL completa en un solo string
url_sftp = "sftp://googlefeedguest:go84gl62fe@transferencias.liverpool.com.mx/decom/googleFeed300K_SB.xml"

try:
    print("🧩 Parseando la URL...")
    url_parseada = urlparse(url_sftp)
    
    # Extraer los datos automáticamente de la URL
    host = url_parseada.hostname
    user = url_parseada.username
    passw = url_parseada.password
    ruta_remota = url_parseada.path
    
    print(f"📡 Servidor detectado: {host}")
    print(f"👤 Usuario detectado: {user}")
    print(f"📂 Ruta del archivo: {ruta_remota}")
    print("\n🔄 Conectando al servidor de Liverpool...")
    
    # Configurar y conectar SSH/SFTP
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=passw, timeout=10)
    
    sftp = ssh.open_sftp()
    print("✅ Conexión exitosa.")
    
    # Validar si el archivo existe
    print(f"🔍 Buscando el archivo en el servidor...")
    info_archivo = sftp.stat(ruta_remota)
    
    print("\n🎉 ¡TODO FINE! El archivo existe en esa ruta y la conexión es correcta.")
    print(f"📊 Tamaño del archivo: {info_archivo.st_size} bytes")
    
    sftp.close()
    ssh.close()

except FileNotFoundError:
    print("\n❌ Error: Conectó bien, pero el archivo NO existe en esa ruta.")
except paramiko.AuthenticationException:
    print("\n❌ Error: Credenciales incorrectas.")
except Exception as e:
    print(f"\n❌ Error de conexión: {e}")