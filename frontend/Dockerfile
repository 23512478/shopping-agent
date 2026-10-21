FROM node:20-alpine AS build

WORKDIR /app
COPY package.json ./
RUN npm config set registry https://registry.npmmirror.com && npm install
COPY . .
RUN npm run build

FROM nginx:alpine
COPY --from=build /app/dist /usr/share/nginx/html
# SPA 路由兜底：直接访问/刷新 /search、/product/:id 等前端路由不能 404
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
