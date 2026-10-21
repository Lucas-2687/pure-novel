"""Local interactive setup for the selected project; stores no credentials."""
import getpass
import os
import warnings
from urllib.parse import urlsplit

import cloud
import manage_user


def main():
    print('纯阅 · 创建首个受邀账号')
    project_url = input('Supabase 项目 HTTPS 地址：').strip().rstrip('/')
    try:
        parsed = urlsplit(project_url)
        valid = (parsed.scheme == 'https' and parsed.hostname and not parsed.username and
                 not parsed.password and parsed.port is None and not parsed.path and
                 not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise cloud.CloudError(400, '请输入不含路径、端口或账号信息的项目 HTTPS 地址。')
    username = cloud.normalize_username(input('用户名：'))
    print('项目：' + project_url)
    print('用户名：' + username)
    print('密钥和密码仅在本机隐藏输入，不保存到文件。输入时看不到字符是正常现象。')
    print('请从该项目 Settings → API Keys 复制 Secret key。不要使用其他项目的密钥。')
    # Refuse getpass's visible-input fallback (e.g. a redirected terminal).
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        secret = getpass.getpass('粘贴 Secret key 后回车：').strip()
        if not secret.startswith(('sb_secret_', 'eyJ')):
            raise cloud.CloudError(400, '密钥格式无效，未创建账号。')
        password = getpass.getpass('设置纯阅专用密码（至少 12 位）：')
        if password != getpass.getpass('再次输入密码：'):
            raise cloud.CloudError(400, '两次密码不一致，未创建账号。')
    names = ('SUPABASE_URL', 'SUPABASE_PUBLISHABLE_KEY', 'SUPABASE_SECRET_KEY')
    previous = {name: os.environ.get(name) for name in names}
    try:
        os.environ['SUPABASE_URL'] = project_url
        # This short-lived process only makes admin calls. The gateway's config
        # requires a default key, which is never exposed to a browser here.
        os.environ['SUPABASE_PUBLISHABLE_KEY'] = secret
        os.environ['SUPABASE_SECRET_KEY'] = secret
        user = manage_user.create_user(username, password)
        print(f"开户成功：{user['username']}（{user['id']}），已授予成员权限。")
        print('此步骤仅创建 Supabase 账号；网站部署请参阅 DEPLOY.md。')
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


if __name__ == '__main__':
    try:
        main()
    except (cloud.CloudError, getpass.GetPassWarning) as exc:
        print('未完成：' + str(exc))
    except (KeyboardInterrupt, EOFError):
        print('\n已取消。')
    finally:
        input('按回车关闭窗口。')