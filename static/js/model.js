const addModelBtn = document.getElementById("btnAddModelQuick")
const modelName = document.getElementById("addmodel-name")
const modelPath = document.getElementById("addmodel-path")

addModelBtn.addEventListener("click" , function(e){
    const URL = "http://localhost:8000"
    const modName = modelName.value
    const modPath = modelPath.value
    fetch(`${URL}/api/models` , {method:"POST" , body:JSON.stringify({name:modName , model_path:modPath}) , headers:{
        "Content-Type":"application/json"
    }})
    .then((response) => response.json())
    .then((data) => alert("Model Added"))
    .catch((err) => alert(err))
})