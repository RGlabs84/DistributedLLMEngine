from engine.config import get_base_dir
import os
import io
import zipfile
import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes
import shutil

def derive_key(password: str, salt: bytes) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=480000,
    )
    return kdf.derive(password.encode())

def create_master_vault(password: str, output_path: str = "DLNE_Master_Backup.enc"):
    """
    Bundles all engine configuration (config/, wg_configs/, models.json) into an AES-256-GCM encrypted vault.
    """
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zipf:
        # Core config directory files
        config_dir = os.path.join(get_base_dir(), "config")
        if os.path.exists(config_dir):
            for file in os.listdir(config_dir):
                file_path = os.path.join(config_dir, file)
                if os.path.isfile(file_path) and not file.endswith(".enc") and not file.endswith(".py"):
                    zipf.write(file_path, arcname=file)
                    
        # Grab models.json if it exists inside models/
        models_json = os.path.join(get_base_dir(), "models", "models.json")
        if os.path.exists(models_json):
            zipf.write(models_json, arcname="models.json")
                
        # Wireguard keys/configs directory
        wg_dir = os.path.join(get_base_dir(), "wg_configs")
        if os.path.exists(wg_dir) and os.path.isdir(wg_dir):
            for root, _, files in os.walk(wg_dir):
                for file in files:
                    file_path = os.path.join(root, file)
                    arcname = os.path.relpath(file_path, get_base_dir())
                    zipf.write(file_path, arcname=arcname)
                
    zip_data = zip_buffer.getvalue()
    
    salt = os.urandom(16)
    key = derive_key(password, salt)
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, zip_data, None)
    
    out_path = output_path if os.path.isabs(output_path) else os.path.join(get_base_dir(), output_path)
    with open(out_path, "wb") as f:
        f.write(salt)
        f.write(nonce)
        f.write(ciphertext)

def restore_master_vault(password: str, input_path: str = "DLNE_Master_Backup.enc") -> bool:
    """
    Decrypts the master vault and completely restores the engine state (configs, invites, chat keys, etc.).
    Returns True if successful, False if password incorrect or file corrupted.
    """
    try:
        path = input_path if os.path.isabs(input_path) else os.path.join(get_base_dir(), input_path)
        if not os.path.exists(path):
            fallback = os.path.join(get_base_dir(), "config", os.path.basename(input_path))
            if os.path.exists(fallback):
                path = fallback

        with open(path, "rb") as f:
            data = f.read()
            
        if len(data) < 28:
            return False 
            
        salt = data[:16]
        nonce = data[16:28]
        ciphertext = data[28:]
        
        key = derive_key(password, salt)
        aesgcm = AESGCM(key)
        
        try:
            zip_data = aesgcm.decrypt(nonce, ciphertext, None)
        except Exception:
            return False
            
        # We successfully decrypted it. Now we restore.
        # Clean config/ directory except the vault itself and unlock scripts
        config_dir = os.path.join(get_base_dir(), "config")
        if os.path.exists(config_dir):
            for file in os.listdir(config_dir):
                f_path = os.path.join(config_dir, file)
                if os.path.isfile(f_path) and not file.endswith(".enc") and not file.endswith(".py"):
                    os.remove(f_path)
                    
        if os.path.exists(os.path.join(get_base_dir(), "wg_configs")): 
            shutil.rmtree(os.path.join(get_base_dir(), "wg_configs"))
            
        zip_buffer = io.BytesIO(zip_data)
        with zipfile.ZipFile(zip_buffer, "r") as zipf:
            for member in zipf.namelist():
                # Normalize path separators
                member_norm = member.replace("\\", "/")
                
                # Route files correctly back to disk
                if member_norm.startswith("wg_configs/") or "wg_configs" in member_norm:
                    zipf.extract(member, get_base_dir())
                elif member_norm == "models.json":
                    m_dir = os.path.join(get_base_dir(), "models")
                    os.makedirs(m_dir, exist_ok=True)
                    with open(os.path.join(m_dir, "models.json"), "wb") as f:
                        f.write(zipf.read(member))
                else:
                    os.makedirs(config_dir, exist_ok=True)
                    member_name = member_norm.split("/")[-1]
                    with open(os.path.join(config_dir, member_name), "wb") as f:
                        f.write(zipf.read(member))
            
        return True
    except Exception as e:
        print(f"Vault Unlock error: {e}")
        return False
