"""seeds 子包的包标记。

七份流程 YAML 与加载代码同处一个可导入的包内，加载器才能用
`importlib.resources.files` 读取，从而在打包安装后依然可加载（而非依赖
源码树相对路径）。
"""
