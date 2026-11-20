import os

# 必须在导入 app 之前设置：测试使用内存库
os.environ["BUOY_DB"] = ":memory:"
