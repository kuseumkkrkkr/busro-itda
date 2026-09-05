import {build} from 'esbuild';
import {mkdir,copyFile,cp,readFile,writeFile} from 'node:fs/promises';
await mkdir('dist',{recursive:true});
await build({entryPoints:['app.js'],outfile:'dist/app.js',bundle:true,minify:true,loader:{'.js':'jsx'},define:{'process.env.NODE_ENV':'"production"'}});
await Promise.all(['index.html','style.css'].map(f=>copyFile(f,`dist/${f}`)));
await cp('assets','dist/assets',{recursive:true});
await writeFile('dist/index.html',(await readFile('dist/index.html','utf8')).replace('./dist/app.js','./app.js'));
if(process.env.DEPLOY_BASE){const html=await readFile('dist/index.html','utf8');await writeFile('dist/index.html',html.replace('<head>','<head><base href="'+process.env.DEPLOY_BASE+'">'));}
