# Vale do Orvalho

Protótipo de fazenda 3D em navegador, com câmera isométrica e visual acolhedor inspirado na referência: casinhas de pedra e madeira, telhados azulados, praça de paralelepípedos, vegetação e uma personagem em escala miniatura.

## Executar

Com o Node.js instalado, inicie o servidor local a partir desta pasta:

```text
node server.js
```

Depois acesse `http://localhost:8000`. O Three.js é carregado pelo jsDelivr; é necessária conexão à internet na primeira abertura.

## Controles

- **WASD** ou **setas**: mover a fazendeira
- **E**: plantar, regar ou colher quando estiver perto de um canteiro
- **Clique em um canteiro**: cuidar dele se estiver ao alcance

Uma planta regada amadurece em alguns segundos. A colheita concede moedas e conclui a primeira missão.
