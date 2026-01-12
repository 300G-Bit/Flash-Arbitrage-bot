#!/usr/bin/env python3
"""
代理端口诊断工具
"""
import socket
import requests

def check_port(host, port, timeout=3):
    """检查端口是否开放"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except:
        return False

def test_http_proxy(host, port, timeout=10):
    """测试HTTP代理"""
    proxy = {
        'http': f'http://{host}:{port}',
        'https': f'http://{host}:{port}'
    }
    try:
        response = requests.get(
            'https://httpbin.org/ip',
            proxies=proxy,
            timeout=timeout
        )
        if response.status_code == 200:
            return True, response.json().get('origin', 'unknown')
    except Exception as e:
        return False, str(e)[:50]
    return False, "Unknown error"

def test_socks5_proxy(host, port, timeout=10):
    """测试SOCKS5代理"""
    proxy = {
        'http': f'socks5://{host}:{port}',
        'https': f'socks5://{host}:{port}'
    }
    try:
        response = requests.get(
            'https://httpbin.org/ip',
            proxies=proxy,
            timeout=timeout
        )
        if response.status_code == 200:
            return True, response.json().get('origin', 'unknown')
    except Exception as e:
        return False, str(e)[:50]
    return False, "Unknown error"

def main():
    print("=" * 60)
    print("         Clash 代理端口诊断工具")
    print("=" * 60)
    
    host = "127.0.0.1"
    
    # 常见的Clash端口
    common_ports = [7890, 7891, 7892, 7893, 7897, 7898, 1080, 1081, 10808, 10809]
    
    print(f"\n📍 检查本地端口开放情况 ({host}):\n")
    
    open_ports = []
    for port in common_ports:
        status = "✓ 开放" if check_port(host, port) else "✗ 关闭"
        if check_port(host, port):
            open_ports.append(port)
        print(f"   端口 {port}: {status}")
    
    if not open_ports:
        print("\n❌ 没有检测到开放的代理端口!")
        print("   请确认 Clash 是否已启动")
        return
    
    print(f"\n📡 检测到开放端口: {open_ports}")
    
    # 测试HTTP代理
    print(f"\n🔍 测试HTTP代理功能:\n")
    for port in open_ports:
        print(f"   测试 {host}:{port} (HTTP)...", end=" ")
        success, info = test_http_proxy(host, port)
        if success:
            print(f"✓ 成功 (出口IP: {info})")
        else:
            print(f"✗ 失败 ({info})")
    
    # 测试SOCKS5代理
    print(f"\n🔍 测试SOCKS5代理功能:\n")
    for port in open_ports:
        print(f"   测试 {host}:{port} (SOCKS5)...", end=" ")
        success, info = test_socks5_proxy(host, port)
        if success:
            print(f"✓ 成功 (出口IP: {info})")
        else:
            print(f"✗ 失败 ({info})")
    
    # 测试连接币安
    print(f"\n🔍 测试连接币安API:\n")
    binance_endpoints = [
        "https://fapi.binance.com/fapi/v1/ping",
        "https://api.binance.com/api/v3/ping",
    ]
    
    for port in open_ports:
        proxy = {
            'http': f'http://{host}:{port}',
            'https': f'http://{host}:{port}'
        }
        for endpoint in binance_endpoints:
            print(f"   {host}:{port} → {endpoint.split('/')[2]}...", end=" ")
            try:
                response = requests.get(endpoint, proxies=proxy, timeout=10)
                if response.status_code == 200:
                    print("✓ 成功")
                else:
                    print(f"✗ 状态码 {response.status_code}")
            except requests.exceptions.ProxyError:
                print("✗ 代理错误")
            except requests.exceptions.ConnectTimeout:
                print("✗ 超时")
            except Exception as e:
                print(f"✗ {type(e).__name__}")
    
    # 建议配置
    print("\n" + "=" * 60)
    print("📋 建议的脚本配置:")
    print("=" * 60)
    
    if open_ports:
        suggested_port = open_ports[0]
        print(f"""
在 test_pin_realtime.py 中修改:

PROXY_HOST = "127.0.0.1"
PROXY_HTTP_PORT = {suggested_port}
PROXY_SOCKS5_PORT = {suggested_port}
USE_PROXY = True
""")
    
    print("\n💡 提示:")
    print("   - 如果HTTP和SOCKS5测试都成功，说明是混合端口")
    print("   - 如果只有HTTP成功，只用HTTP代理即可")
    print("   - 确保Clash的'Allow LAN'已开启")

if __name__ == "__main__":
    main()
