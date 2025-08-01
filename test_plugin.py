#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试插件配置是否正确
"""

import sys
import os
import json

# 添加 AstrBot 路径
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'AstrBot'))

try:
    from astrbot.core.config.astrbot_config import AstrBotConfig
    from astrbot.core.star import Context
    print("✅ AstrBot 模块导入成功")
except ImportError as e:
    print(f"❌ AstrBot 模块导入失败: {e}")
    sys.exit(1)

def test_config_schema():
    """测试配置 schema"""
    print("\n🔧 测试配置 schema...")
    
    schema_path = os.path.join(os.path.dirname(__file__), '_conf_schema.json')
    if not os.path.exists(schema_path):
        print("❌ _conf_schema.json 文件不存在")
        return False
    
    try:
        with open(schema_path, 'r', encoding='utf-8') as f:
            schema = json.load(f)
        print("✅ schema 文件格式正确")
        
        # 检查必要字段
        required_fields = ['api_base_url', 'timeout', 'poll_interval']
        for field in required_fields:
            if field not in schema:
                print(f"❌ 缺少必要字段: {field}")
                return False
            print(f"✅ 字段 {field} 存在")
        
        return True
    except json.JSONDecodeError as e:
        print(f"❌ schema 文件格式错误: {e}")
        return False

def test_plugin_config():
    """测试插件配置加载"""
    print("\n🔧 测试插件配置加载...")
    
    schema_path = os.path.join(os.path.dirname(__file__), '_conf_schema.json')
    config_path = os.path.join(os.path.dirname(__file__), 'test_config.json')
    
    try:
        with open(schema_path, 'r', encoding='utf-8') as f:
            schema = json.load(f)
        
        # 创建 AstrBotConfig 实例
        config = AstrBotConfig(
            config_path=config_path,
            schema=schema
        )
        print("✅ AstrBotConfig 实例创建成功")
        
        # 测试配置访问
        api_base_url = config.get('api_base_url', 'default')
        timeout = config.get('timeout', 30)
        poll_interval = config.get('poll_interval', 5)
        
        print(f"✅ 配置读取成功:")
        print(f"   - api_base_url: {api_base_url}")
        print(f"   - timeout: {timeout}")
        print(f"   - poll_interval: {poll_interval}")
        
        return True
    except Exception as e:
        print(f"❌ 配置加载失败: {e}")
        return False

def test_plugin_import():
    """测试插件导入"""
    print("\n🔧 测试插件导入...")
    
    try:
        # 模拟插件导入
        plugin_dir = os.path.dirname(__file__)
        sys.path.insert(0, plugin_dir)
        
        import main
        print("✅ 插件模块导入成功")
        
        # 检查插件类
        if hasattr(main, 'Main'):
            print("✅ 插件类 Main 存在")
            return True
        else:
            print("❌ 插件类 Main 不存在")
            return False
    except Exception as e:
        print(f"❌ 插件导入失败: {e}")
        return False

def main():
    print("🚀 开始测试 RepoInsight 插件...")
    
    tests = [
        test_config_schema,
        test_plugin_config,
        test_plugin_import
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
    
    print(f"\n📊 测试结果: {passed}/{total} 通过")
    
    if passed == total:
        print("🎉 所有测试通过！插件配置正确。")
    else:
        print("❌ 部分测试失败，请检查配置。")
        sys.exit(1)

if __name__ == '__main__':
    main()