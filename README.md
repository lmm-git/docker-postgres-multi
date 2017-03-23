# docker-postgres-multi

docker container which can handle multiple postgres databases and users instead of just one like the offical image.

## Usage

docker run --name postgres-mutli -e POSTGRES_USERS="user1:pass1|user2:pass2|user3:pass3" -e POSTGRES_DATABASES="database1:user1|database2:user2|database2:user3" -it --rm lmmdock/postgres-multi
