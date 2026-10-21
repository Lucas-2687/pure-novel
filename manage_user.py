"""Administrator-only account creation. Never expose this as an HTTP route."""
import argparse
import getpass
import re

import cloud


def create_user(username, password):
    email = cloud.username_email(username)
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise cloud.CloudError(400, '新密码需为 12～256 个字符。')
    user = cloud.remote('/auth/v1/admin/users', admin=True, data={
        'email': email, 'password': password, 'email_confirm': True,
        'user_metadata': {'username': cloud.normalize_username(username)},
    })
    uid = user.get('id', '')
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', uid):
        raise cloud.CloudError(502, '账号服务未返回有效用户 ID；请检查控制台，勿重复创建。')
    try:
        cloud.remote('/rest/v1/members', admin=True, data={'user_id': uid, 'active': True})
    except cloud.CloudError:
        raise cloud.CloudError(503, f'账号已创建，但成员授权未完成。请在 members 中为用户 {uid} 设置 active=true；不要重复开户。') from None
    return {'id': uid, 'username': cloud.normalize_username(username)}


def main():
    parser = argparse.ArgumentParser(description='创建纯阅受邀账号；密码仅在本机隐藏输入。')
    parser.add_argument('username', help='3～32 位中文、字母、数字或下划线，以中文或字母开头')
    args = parser.parse_args()
    try:
        username = cloud.normalize_username(args.username)
        cloud.config()
        password = getpass.getpass('设置纯阅专用密码（至少 12 位）：')
        if password != getpass.getpass('再次输入密码：'):
            parser.exit(1, '两次密码不一致，未创建账号。\n')
        user = create_user(username, password)
    except cloud.CloudError as exc:
        parser.exit(1, str(exc) + '\n')
    print(f"已创建受邀账号：{user['username']}（{user['id']}）")


if __name__ == '__main__':
    main()