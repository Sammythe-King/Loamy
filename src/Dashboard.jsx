import React from "react";
import "./Dashboard.css";
import ExpenseChart from "./ExpenseChart";
import { FontAwesomeIcon } from "@fortawesome/react-fontawesome";
import { faBell, faCheck, faClock, faGear, faJetFighter, faMessage, faReceipt, faRightFromBracket, faWallet  } from "@fortawesome/free-solid-svg-icons";
import { faArrowLeft , fa} from "@fortawesome/free-solid-svg-icons/faArrowLeft";
import { faGoogleWallet } from "@fortawesome/free-brands-svg-icons";
import ProgressBar from  './ProgressBar'
import dashboardData from "./DahboardData";
import { useNavigate } from "react-router-dom";
import {FaWallet} from "react-icons/fa";
// import {Wallet} from '@heroicons/react/24/solid';



// const { invoices, notifications, summary } = dashboardData;
function getGreeting() {
  const hour = new Date().getHours();
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}
const Dashboard = () => {

  


  const navigate = useNavigate();
  const handleLogout = () => {
    // Perform logout logic here
    navigate("/");
  }

  const { invoices, notifications, summary } = dashboardData;
  
   const ProgressValue = 10;  
  // const value =  ProgressBar.value;
  const formatDate = (date) => {
    return new Date(date).toLocaleDateString("en-NG", {
      month: "short",
      day: "numeric",
    });
  };
  return (
    <div style={{ padding: "0 20px" }} className="Dash1">
      
     
      <nav style={{ display: "flex", gap:'85%', padding:"0 20px" }} className="thhenav">
        <div>
          <h3>Loamy</h3>
        </div>

        <div style={{ display: "flex", gap: "20px", alignItems: "center" }}>
            {/* <div style={{ display: "flex", gap: "5px", }}>
              <i className="fa-regular fa-file-arrow-up" style={{position:'relative', top:'20px'}}></i>
              <p>Upload</p>
          </div> */}

          <div>
            <i className="fa-regular fa-bell"></i>
          </div>

          <div>
            <FontAwesomeIcon icon={faGear} />
          </div>

          <div onClick={handleLogout} style={{cursor:"pointer"}}>
            <i className="fa-solid fa-right-from-bracket"></i>
          </div>

         
        </div>
      </nav>

     
      <div style={{ marginBottom: "5%" }} className="greetings">
        <h1 >{getGreeting()}, 👋</h1>
        <p>Here's your financial overview for today</p>
      </div>

     
      <div
        style={{
          display: "flex",
          marginBottom: "5%",
          justifyContent: "space-between",

        }}
        className="originsection"
      >
        
        <div className="cashflow" >
          <div style={{display: "flex", gap: "15px", alignItems: "center"}}>
           <FontAwesomeIcon icon={faWallet} style={{color:'green',}} />
          <p style={{ fontSize:"20px"}}>Cash Flow Overview</p>
          </div>

          <div style={{ display: "flex",gap:"30px"}} className="cheifofCashflow">



            <span style={{borderRadius: "20px",}} className="cashin1 cashino">
              <div style={{display: "flex", gap: "5px", alignItems: "center",fontSize:"15px"}}>
                 <FontAwesomeIcon icon={faWallet} style={{color:'green'}}/>
              <p>Cash In</p>
              </div>
              {/* <FontAwesomeIcon icon={faArrowDownLeft} /> */}
              <span className="cashin1p2">200,000</span>
            </span>



          
            
            <span style={{borderRadius: "20px",}} className="cashin2 cashino testcahsin ">
              <div style={{display: "flex", gap: "5px", alignItems: "center",fontSize:"15px"}}>
                 <FontAwesomeIcon icon={faWallet} style={{color:'red'}}/>
              <p>Cash Out</p>
              </div>
               
              <span className="cashin2p2" >200,000</span>
            </span>




            <span style={{ borderRadius: "20px",}} className="cashin3  cashino testcahsin">
              <div style={{display: "flex", gap: "5px", alignItems: "center",fontSize:"15px"}}>
              <FontAwesomeIcon icon={faWallet} style={{color:'green'}}/>
              <p>Current Cash</p>
              </div>
              <span className="cashin1p2" >200,000</span>
            </span>
         


          <div className="responsivecashflow" >
            
            <span style={{borderRadius: "20px",}} className="cashin2 cashino cashin44 cashin22">
              <div style={{display: "flex", gap: "5px", alignItems: "center",fontSize:"15px"}}>
                 <FontAwesomeIcon icon={faWallet} style={{color:'red'}}/>
              <p>Cash Out</p>
              </div>
               
              <span className="cashin2p2" >200,000</span>
            </span>




            <span style={{ borderRadius: "20px",}} className="cashin3  cashino cashin44">
              <div style={{display: "flex", gap: "5px", alignItems: "center",fontSize:"15px"}}>
              <FontAwesomeIcon icon={faWallet} style={{color:'green'}}/>
              <p>Current Cash</p>
              </div>
              <span className="cashin1p2" >200,000</span>
            </span>
          </div>

           {/* <div style={{background:"red",}} >
              <div style={{display:"flex"}}>
              <FontAwesomeIcon icon={faWallet} style={{color:'green', fontSize:'10px'}}/>
              <p>Current Cash</p>
              </div>
              <p className="cashin1p2" >200,000</p>
            </div> */}


          </div>
        </div>

      
        <div className="businessrunway" > 
          <div style={{display: "flex", gap: "5px", alignItems: "center"}}>
          <FontAwesomeIcon icon={faClock} style={{color:"#768E52"}}/>
          <h3>Business Runway</h3>
          </div>

          <span style={{color:"#768E52", fontSize:"30px",fontWeight:"600" }} >{ProgressValue} Days</span>
          <p>
            At your current spending rate, you may run out of cash by
          </p>
          <ProgressBar value={ProgressValue}  />
        </div>


      </div>



        <div>

        <div style={{
          display:"flex",justifyContent:"space-between",gap:"20px", flexWrap:"wrap"
        }}>
          
            <div className="Expenses Expenses22" style={{width: "500px"}}> 

              <h2>Expense Breakdown</h2>
              <ExpenseChart style={{ margin: "0 0" }} />

            </div>



            <div className="Expenses invoicessss" >

                <div style={{display: "flex", gap: "5px", alignItems: "center"}}>
                <span><FontAwesomeIcon icon={faReceipt}/></span>
                <p style={{fontSize:"20px"}}>Invoice Tracker</p>
                </div>


                {/* 🧾 Invoices */}
              {invoices.map((inv) => (
                <div key={inv.id} style={{borderRadius:"20px", padding:"3px 10px", border:"1px solid rgba(0, 0, 0, 0.122)",marginBottom:"10px", display:"flex", gap:"150px"}} className="invoiic" >

                <div>

                <h4>{inv.client.name}</h4>
                <p style={{opacity:"0.7", fontSize:"10px"}}>Due: {formatDate(inv.dueDate)}</p>

                </div>

                <div style={{position:"relative", top:"30px"}} className="invoicestatus" > 

                <span style={{marginRight:"20px"}}>₦{inv.amount.toLocaleString()}</span>
              
                <span style={{textTransform:"lowercase"}}>{inv.status}</span>
                </div>

                </div>
                ))}



            </div>  

              <div className="Expenses">
                <div style={{display: "flex", gap: "5px", alignItems: "center"}}>
                <FontAwesomeIcon icon={faBell}/>
               <p style={{fontSize:"20px"}}>
                Recent Notification
               </p>
                </div>
                {notifications.map((note) => (
                <div key={note.id} className="notification" style={{borderRadius:"20px", padding:"3px 10px", border:"1px solid rgba(0, 0, 0, 0.122)",marginBottom:"10px"}}>
                  <p>{note.message}</p>
                  <p>{note.createdAt}</p>
              </div>
              ))}

          </div>


        </div>
     
        </div>

     

     
      {/* <div className="lastsection" style={{ marginTop: "5%" }}>
        <div className="firstsection">
          <p>Ask Loamy </p>
        </div>

        <div>
          <input
            type="text"
            placeholder="Ask me anything about your finances"
            style={{ width: "93%", padding: "10px", borderRadius: "13px", border: "1px solid #ccc", }}
          />
          <button style={{ marginLeft: "10px", padding: "10px 15px", borderRadius: "5px", border: "1px solid #ccc" }}>
            <FontAwesomeIcon icon={faJetFighter} />
          </button>
        </div>

        
        <div className="thirdsection" >
          <p>Can I hire more sales assistants?</p>
          
         
          <p>What's my biggest expense this month?</p>
          <p>How much do customers owe me?</p>
          
          

          
        </div>
         <div className="thirdsectionbigscreen" >
          <p>Can I hire more sales assistants?</p>
          <span className="thirdsectionbigscreenspan">

          <p>What's my biggest expense this month?</p>
          <p>How much do customers owe me?</p>
          </span>
          
         

          
        </div>
      </div> */}
    </div>
  );
};

export default Dashboard;