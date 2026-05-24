# -*- coding: utf-8 -*-
import os
import sqlite3
import shutil
import re

def main():
    # 获取基本路径
    base_dir = os.path.dirname(os.path.abspath(__file__))
    data_dir = os.path.join(base_dir, '..', 'data')
    db_path = os.path.join(data_dir, 'experts.db')
    photos_dir = os.path.join(base_dir, 'static', 'uploads', 'expert_photos')
    
    print("="*60)
    print("🚀 评标专家身份证照片一键自动检测与修复系统")
    print(f"📂 数据库路径: {os.path.abspath(db_path)}")
    print(f"📂 照片物理目录: {os.path.abspath(photos_dir)}")
    print("="*60)
    
    if not os.path.exists(db_path):
        print("❌ 错误: 专家数据库不存在！")
        return
    if not os.path.exists(photos_dir):
        print("❌ 错误: 照片物理目录不存在！")
        return
        
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    
    c.execute("SELECT id, name, phone, photo_path FROM experts")
    experts = c.fetchall()
    
    # 扫描 photos_dir 目录下的所有文件，用以后续匹配好图
    all_files = []
    try:
        all_files = os.listdir(photos_dir)
    except Exception as e:
        print(f"❌ 读取照片目录失败: {e}")
        return
        
    broken_count = 0
    repaired_count = 0
    
    for exp in experts:
        name = exp['name']
        phone = exp['phone']
        photo_path_str = exp['photo_path']
        
        if not photo_path_str or photo_path_str == 'None':
            continue
            
        paths = photo_path_str.split(',')
        is_broken = False
        broken_files = []
        
        for p in paths:
            p = p.strip()
            if not p:
                continue
            filename = os.path.basename(p)
            phys_path = os.path.join(photos_dir, filename)
            
            # 判断文件是否损坏或不存在：
            # 1. 物理文件不存在
            # 2. 文件大小极小（小于2KB，通常为损坏的文件或 Mac 垃圾元数据文件）
            if not os.path.exists(phys_path) or os.path.getsize(phys_path) < 2048:
                is_broken = True
                broken_files.append((p, phys_path, filename))
                
        if is_broken:
            broken_count += 1
            print(f"\n⚠️ 发现损坏/缺失照片的专家: {name} (手机: {phone})")
            for web_p, phys_p, fname in broken_files:
                reason = "文件缺失" if not os.path.exists(phys_p) else f"文件损坏 (大小: {os.path.getsize(phys_p)} 字节)"
                print(f"  - 损坏文件: {fname} ({reason})")
                
                # 尝试在照片目录寻找好图进行自动修复
                # 好图规则：文件名包含专家姓名和手机号，且文件大小大于20KB，且名字不是当前损坏的文件名
                found_good_file = None
                good_file_size = 0
                
                for f in all_files:
                    if f == fname:
                        continue
                    # 匹配姓名和手机号，且大小要足够大
                    if name.lower() in f.lower() and phone in f:
                        f_path = os.path.join(photos_dir, f)
                        if os.path.exists(f_path):
                            f_size = os.path.getsize(f_path)
                            if f_size > 20480: # 必须大于 20KB 确保是正常照片而非垃圾元数据
                                if f_size > good_file_size:
                                    found_good_file = f
                                    good_file_size = f_size
                                    
                if found_good_file:
                    good_path = os.path.join(photos_dir, found_good_file)
                    print(f"  ✨ 匹配到历史完好备份照片: '{found_good_file}' ({good_file_size // 1024} KB)")
                    try:
                        # 执行自动修复：复制好图覆盖损坏的照片
                        shutil.copy2(good_path, phys_p)
                        print(f"  ✅ 成功修复: 已使用好图覆盖损坏的文件！")
                        repaired_count += 1
                    except Exception as ex:
                        print(f"  ❌ 修复失败 (无法复制文件): {ex}")
                else:
                    print("  🔍 遗憾: 在照片目录下未找到可用于恢复的历史备份照片。")
                    
    print("\n" + "="*60)
    print("📊 修复扫描总结:")
    print(f"  - 共扫描专家记录: {len(experts)} 条")
    print(f"  - 发现异常/损坏照片的专家: {broken_count} 位")
    print(f"  - 自动定位好图并成功修复: {repaired_count} 次")
    print("="*60)
    
    conn.close()

if __name__ == '__main__':
    main()
