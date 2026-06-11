// ...existing code...
import React, { useState } from 'react';
import './LoamyHomePage.css'
import { motion } from 'framer-motion';

import dashB from './assets/dashB.jpg'
import Chat from './assets/Chat.jpg'
import Tracking from './assets/Tracking.jpg'
import Assistant from './assets/Assistant.jpg'
import CashFlow from './assets/CashFlow.jpg'
import ExpenseCat from './assets/ExpenseCat.jpg'
import SmartNotifcation from './assets/SmartNotifcation.jpg'
import patterns from './assets/patterns.jpg'
import Prediction from './assets/Prediction.jpg'
import {FaAddressCard, FaArrowRight, FaBell, FaCheck, FaDatabase, FaFile, FaFileInvoice, FaFileUpload, FaGalacticSenate, FaGasPump, FaGoogleWallet, FaInbox, FaLayerGroup, FaMailBulk, FaMailchimp, FaPen, FaPenAlt, FaReceipt, FaRegMoneyBillAlt, FaRegStar, FaRobot, FaStar, FaStarAndCrescent, FaStarOfLife, FaTag, FaTags, FaUpload, FaUser, FaWallet,FaBars,FaTimes} from "react-icons/fa";
import LoamyLogo from './assets/LoamyLogo.png'

// ── Reusable animation variants ──────────────────────────────────────────────

const fadeUp = {
  hidden: { opacity: 0, y: 60 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.65, ease: 'easeOut' } },
};

const fadeDown = {
  hidden: { opacity: 0, y: -60 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.65, ease: 'easeOut' } },
};

const fadeLeft = {
  hidden: { opacity: 0, x: -80 },
  visible: { opacity: 1, x: 0, transition: { duration: 0.65, ease: 'easeOut' } },
};

const fadeRight = {
  hidden: { opacity: 0, x: 80 },
  visible: { opacity: 1, x: 0, transition: { duration: 0.65, ease: 'easeOut' } },
};

const fadeScale = {
  hidden: { opacity: 0, scale: 0.85 },
  visible: { opacity: 1, scale: 1, transition: { duration: 0.6, ease: 'easeOut' } },
};

const staggerContainer = {
  hidden: {},
  visible: { transition: { staggerChildren: 0.12, delayChildren: 0.1 } },
};

const staggerContainerFast = {
  hidden: {},
  visible: { transition: { staggerChildren: 0.08, delayChildren: 0.05 } },
};

// Viewport config reused across all motion elements
const vp = { once: true, amount: 0.2 };

// ── Alternating directional variants for feature cards ───────────────────────
const cardDirections = [fadeLeft, fadeUp, fadeRight, fadeDown, fadeLeft, fadeRight];

// ── Component ────────────────────────────────────────────────────────────────

const LoamyHomePage = () => {
  const [mobileOpen, setMobileOpen] = useState(false);
  const toggleMobile = () => setMobileOpen(v => !v);

  return (
    <div className='ogaLoamyLandging'>

      {/* ── Desktop Nav ── */}
      <motion.nav
        className='TheLandNav'
        initial="hidden"
        animate="visible"
        variants={fadeDown}
      >
        <motion.div style={{ display: "flex" }} variants={fadeLeft} initial="hidden" animate="visible">
          <img src={LoamyLogo} alt="" width={40} />
          <p>Loamy</p>
        </motion.div>
        <motion.div
          style={{ display: "flex" }}
          className='navsubssbsbdbdb'
          variants={staggerContainer}
          initial="hidden"
          animate="visible"
        >
          {['Features', 'How it works', 'Pricing', 'About'].map((item) => (
            <motion.p key={item} variants={fadeDown}>{item}</motion.p>
          ))}
        </motion.div>
        <motion.div
          style={{ display: "flex" }}
          className='navsubssbsbdbdb'
          variants={staggerContainer}
          initial="hidden"
          animate="visible"
        >
          <motion.p variants={fadeDown}>Login</motion.p>
          <motion.p variants={fadeDown}>Get Started Free</motion.p>
        </motion.div>
      </motion.nav>

      {/* ── Mobile Nav ── */}
      <nav className="MobileNav" role="navigation" aria-label="Mobile navigation">
        <motion.div
          className="mobileBrand"
          initial={{ opacity: 0, x: -40 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ duration: 0.5 }}
        >
          <img src={LoamyLogo} alt="Loamy" width={36} />
          <p>Loamy</p>
        </motion.div>
        <motion.button
          className="mobileToggle"
          onClick={toggleMobile}
          aria-label="Toggle menu"
          initial={{ opacity: 0, x: 40 }}
          animate={{ opacity: 1, x: 0 }}
          transition={{ duration: 0.5 }}
        >
          {mobileOpen ? <FaTimes /> : <FaBars />}
        </motion.button>
        <div className={`mobileMenu ${mobileOpen ? 'open' : ''}`}>
          <a href="#features" onClick={() => setMobileOpen(false)}>Features</a>
          <a href="#how" onClick={() => setMobileOpen(false)}>How it works</a>
          <a href="#pricing" onClick={() => setMobileOpen(false)}>Pricing</a>
          <a href="#about" onClick={() => setMobileOpen(false)}>About</a>
          <a href="#login" className="mobileLogin" onClick={() => setMobileOpen(false)}>Login</a>
          <button className="mobileCTA" onClick={() => setMobileOpen(false)}>Get Started Free</button>
        </div>
      </nav>

      {/* ── Hero Section ── */}
      <section className='FirstoneSection'>
        <motion.div
          className='SubsssFirstoneSection'
          variants={staggerContainer}
          initial="hidden"
          animate="visible"
        >
          <motion.span
            style={{ backgroundColor: "white", padding: "5px 10px", borderRadius: "20px", opacity: "0.8", fontWeight: "200" }}
            className='firstspanofsfjfns'
            variants={fadeLeft}
          >
            AI finacial awareness, built for African SMBs
          </motion.span>

          <motion.h2 variants={fadeUp}>
            Your business finances, <span>finally made clear.</span>
          </motion.h2>

          <motion.p
            style={{ width: "30vw", lineHeight: '20px', marginBottom: "20px" }}
            variants={fadeLeft}
          >
            Loamy automatically tracks your cash flow, expenses, and invoices from emails and bank alerts then gives you smart insights so you can make better business decisions.
          </motion.p>

          <motion.div className='sssdFirstoneSectionButtons' variants={fadeUp}>
            <motion.button
              style={{ backgroundColor: "#48662a", color: "white" }}
              whileHover={{ scale: 1.05, backgroundColor: "#3a5220" }}
              whileTap={{ scale: 0.97 }}
              transition={{ type: 'spring', stiffness: 300 }}
            >
              Get Started Free <FaArrowRight size={10} />
            </motion.button>
            <motion.button
              style={{ backgroundColor: "white", color: "black" }}
              whileHover={{ scale: 1.05 }}
              whileTap={{ scale: 0.97 }}
              transition={{ type: 'spring', stiffness: 300 }}
            >
              See How it Works
            </motion.button>
          </motion.div>

          <motion.div className='sssdFirstoneSectionsmall' variants={staggerContainerFast}>
            {[
              'No Credit card required',
              '14 day free trial',
              'Built for African SMBs'
            ].map((text, i) => (
              <motion.span key={i} variants={fadeUp}>
                <FaCheck size={10} style={{ margin: "0 5px", color: 'green' }} />
                {text}
              </motion.span>
            ))}
          </motion.div>
        </motion.div>

        <motion.div
          style={{ position: "relative", top: "50px" }}
          className='topimghime'
          initial={{ opacity: 0, x: 120, rotate: 3 }}
          animate={{ opacity: 1, x: 0, rotate: 0 }}
          transition={{ duration: 0.9, ease: 'easeOut', delay: 0.3 }}
        >
          <img src={dashB} alt="" width={600} style={{ borderRadius: "30px" }} />
        </motion.div>
      </section>

      {/* ── Data Sources Section ── */}
      <section className='SecondoneSection' style={{ margin: "20% 0" }}>
        <motion.h2
          variants={fadeDown}
          initial="hidden"
          whileInView="visible"
          viewport={vp}
        >
          LOAMY UNDERSTANDS DATA FROM ANYWHERE
        </motion.h2>

        <motion.div
          className='Sub-SecondoneSection'
          variants={staggerContainer}
          initial="hidden"
          whileInView="visible"
          viewport={vp}
        >
          {[
            { icon: <FaWallet />, label: 'Bank Alerts', dir: fadeLeft },
            { icon: <FaWallet />, label: 'Invoice Emails', dir: fadeUp },
            { icon: <FaReceipt />, label: 'Payment Confirmations', dir: fadeDown },
            { icon: <FaFileUpload />, label: 'PDF Uploads', dir: fadeUp },
            { icon: <FaPen />, label: 'Manuel Entry', dir: fadeRight },
          ].map(({ icon, label, dir }, i) => (
            <motion.div key={i} className='sub-SecondoneSection-111' variants={dir}>
              <motion.span
                className='secondSectionIcon'
                whileHover={{ scale: 1.15, rotate: 8 }}
                transition={{ type: 'spring', stiffness: 300 }}
              >
                {icon}
              </motion.span>
              <h4>{label}</h4>
            </motion.div>
          ))}
        </motion.div>
      </section>

      {/* ── How It Works ── */}
      <section className='SecondoneSection' style={{ margin: "20% 0", position: "relative", top: "50px" }}>
        <motion.div
          className='minfitstsubSecondoneSection'
          variants={staggerContainer}
          initial="hidden"
          whileInView="visible"
          viewport={vp}
        >
          <motion.h3 variants={fadeLeft}>HOW IT WORKS</motion.h3>
          <motion.p style={{ fontSize: '30px' }} variants={fadeRight}>
            From scattered messages to <span>financial clarity</span>
          </motion.p>
        </motion.div>

        <motion.div
          className='Sub-SecondoneSection'
          style={{ textAlign: "start", margin: "0 60px", position: "relative", top: '50px' }}
          variants={staggerContainer}
          initial="hidden"
          whileInView="visible"
          viewport={vp}
        >
          {[
            {
              icon: <FaMailBulk />, step: '01', title: 'Connect your email',
              desc: 'Securely link your businesses inbox. Loamy reads bank alerts, invoices and payment confirmations automatically',
              dir: fadeLeft,
            },
            {
              icon: <FaDatabase />, step: '02', title: 'Extract & track',
              desc: 'We extract transactions in seconds — no spreadsheets, no copy pasting, no missed entries',
              dir: fadeUp,
            },
            {
              icon: <FaLayerGroup />, step: '03', title: 'Analyze & categorize',
              desc: 'Expenses are grouped intelligently — rent, inventory, salaries, foodstuff with running totals.',
              dir: fadeDown,
            },
            {
              icon: <FaStarOfLife />, step: '04', title: 'Get insights & alerts',
              desc: 'Receive proactive, plain-English insights about cash flow, runway, and who owes you money.',
              dir: fadeRight,
            },
          ].map(({ icon, step, title, desc, dir }, i) => (
            <motion.div key={i} className="Workssubsss" variants={dir}>
              <div style={{ display: "flex", justifyContent: "space-between" }}>
                <motion.span
                  className='worksiconss'
                  whileHover={{ scale: 1.2, rotate: -10 }}
                  transition={{ type: 'spring', stiffness: 250 }}
                >
                  {icon}
                </motion.span>
                <span style={{ fontSize: '20px' }}>{step}</span>
              </div>
              <h4>{title}</h4>
              <p>{desc}</p>
            </motion.div>
          ))}
        </motion.div>
      </section>

      {/* ── Features Section ── */}
      <section className='ThirdoneSection'>
        <motion.div
          className='minfitstsubThirdoneSection'
          variants={staggerContainer}
          initial="hidden"
          whileInView="visible"
          viewport={vp}
        >
          <motion.span style={{ textAlign: "start" }} variants={fadeLeft}>FEATURES</motion.span>
          <motion.p style={{ fontSize: '50px' }} className='finacbrain' variants={fadeRight}>
            The financial brain your<br /> <span>business deserves</span>
          </motion.p>
        </motion.div>

        <motion.div
          className='Sub-ThirdoneSection'
          variants={staggerContainer}
          initial="hidden"
          whileInView="visible"
          viewport={vp}
        >
          {[
            {
              img: CashFlow, icon: <FaWallet />, title: 'Cash Flow Tracking',
              desc: "See exactly what's coming in and going out, every day synced from your bank alerts.",
            },
            {
              img: ExpenseCat, icon: <FaTags />, title: 'Expense Categorization',
              desc: 'Inventory, rent, salaries, foodstuff — automatically grouped, never miscoded',
            },
            {
              img: Prediction, icon: <FaGalacticSenate />, title: 'Runway Prediction',
              desc: 'Know how many days of cash you have left, before it becomes a problem.',
            },
            {
              img: Tracking, icon: <FaFileInvoice />, title: 'Invoice Tracking',
              desc: "Loamy reads your invoice emails and PDFs, then chases what's owed for you",
            },
            {
              img: SmartNotifcation, icon: <FaBell />, title: 'Smart Notifications',
              desc: 'Gentle, plain-English nudges when something needs your attention — nothing else',
              imgStyle: { padding: "30px", backgroundColor: "white" },
            },
            {
              img: Assistant, icon: <FaRobot />, title: 'AI Financial Assistant',
              desc: 'Ask Loamy anything about your business. Get clear, contextual answers in seconds',
              imgStyle: { padding: '10px', backgroundColor: "white" },
            },
          ].map(({ img, icon, title, desc, imgStyle = {} }, i) => (
            <motion.div
              key={i}
              className='SubssofThirdoneSection'
              variants={cardDirections[i % cardDirections.length]}
              whileHover={{ y: -8, boxShadow: '0 20px 40px rgba(0,0,0,0.12)' }}
              transition={{ type: 'spring', stiffness: 200 }}
            >
              <span style={{ display: "grid", gridTemplateColumns: "repeat(1fr,1)" }}>
                <span style={{ position: "relative", left: "30px" }}>
                  <img
                    src={img} alt="" width={300} height={110}
                    style={{ borderRadius: "40px", border: "4px solid black", backgroundColor: "white", padding: "0px", ...imgStyle }}
                  />
                </span>
                <motion.div
                  className='SubssThirddssIcon'
                  style={{ width: '20px', padding: "10px 20px", borderRadius: "50%" }}
                  whileHover={{ rotate: 15, scale: 1.2 }}
                  transition={{ type: 'spring', stiffness: 300 }}
                >
                  {React.cloneElement(icon, { style: { position: "relative", left: "-8px", top: "2px", color: 'white' } })}
                </motion.div>
              </span>
              <h4>{title}</h4>
              <p>{desc}</p>
            </motion.div>
          ))}
        </motion.div>
      </section>

      {/* ── AI Assistant Section ── */}
      <section className='FourthoneSection'>
        <motion.div
          style={{ position: "relative", left: "-60px" }}
          className='minFourthoneSection'
          variants={staggerContainer}
          initial="hidden"
          whileInView="visible"
          viewport={vp}
        >
          <motion.span variants={fadeLeft}>AI ASSISTANT</motion.span>
          <motion.p style={{ fontSize: "50px" }} className='minFourthoneSectionfirstp' variants={fadeLeft}>
            Ask anything. Get answers <span>grounded in your numbers</span>
          </motion.p>
          <motion.p style={{ width: "40vw", fontWeight: '300' }} variants={fadeUp}>
            Loamy understands your business context — your invoices, expenses, and customers — and replies in plain English. Like a CFO who already knows everything.
          </motion.p>

          <motion.div className='Min-FourthoneSection' variants={staggerContainerFast}>
            <div>
              <motion.span
                style={{ margin: "10px 5px", backgroundColor: "white", padding: "10px", borderRadius: "20px" }}
                variants={fadeLeft}
                whileHover={{ scale: 1.04, x: 6 }}
              >
                Can I hire another sales assistant?
              </motion.span>
              <motion.span
                style={{ margin: "10px 5px", backgroundColor: "white", padding: "10px", borderRadius: "20px" }}
                className='minfffourtlats'
                variants={fadeRight}
                whileHover={{ scale: 1.04, x: -6 }}
              >
                Who owes me money?
              </motion.span>
            </div>
            <motion.span
              style={{ margin: "5px 5px", backgroundColor: "white", padding: "10px", position: "relative", top: "30px", borderRadius: "20px" }}
              className='minfourtlats'
              variants={fadeUp}
              whileHover={{ scale: 1.04 }}
            >
              How much did I spend on foodstuff last month?
            </motion.span>
          </motion.div>
        </motion.div>

        <motion.div
          initial={{ opacity: 0, x: 100, rotate: -4 }}
          whileInView={{ opacity: 1, x: 0, rotate: 0 }}
          viewport={vp}
          transition={{ duration: 0.8, ease: 'easeOut' }}
        >
          <img src={Chat} alt="" width={400} style={{ borderRadius: '60px', border: "4px solid black" }} />
        </motion.div>
      </section>

      {/* ── Testimonials ── */}
      <section style={{ margin: "15% 10%", position: "relative", top: "50px" }} className='lovedsff'>
        <motion.span
          variants={fadeDown}
          initial="hidden"
          whileInView="visible"
          viewport={vp}
        >
          LOVED ACROSS AFRICA
        </motion.span>
        <motion.p
          style={{ fontSize: "40px" }}
          className='lovedsffpp'
          variants={fadeUp}
          initial="hidden"
          whileInView="visible"
          viewport={vp}
        >
          Built with and for <span>real business owners</span>
        </motion.p>

        <motion.div
          style={{ display: "flex", textAlign: 'start' }}
          className='lovedsfffff'
          variants={staggerContainer}
          initial="hidden"
          whileInView="visible"
          viewport={vp}
        >
          {[
            {
              quote: '"Loamy helped us finally understand where our money goes every month"',
              name: 'Amara Okeke', biz: 'Lagos Beauty Supplies. Lagos, Nigeria', dir: fadeLeft,
            },
            {
              quote: '"I stopped chasing invoices manually. Loamy just tells me who owes what"',
              name: 'Kwame Mensah', biz: 'Mensah Print House. Accra, Ghana', dir: fadeUp,
            },
            {
              quote: '"The runway insights saved us. We caught a problem two weeks before it hit"',
              name: 'Zanele Dube', biz: 'Indaba Cafe & Co. Cape Town, SA', dir: fadeRight,
            },
          ].map(({ quote, name, biz, dir }, i) => (
            <motion.div
              key={i}
              className='ForBusness'
              variants={dir}
              whileHover={{ y: -6, boxShadow: '0 16px 32px rgba(0,0,0,0.1)' }}
              transition={{ type: 'spring', stiffness: 180 }}
            >
              <span>{quote}</span>
              <div style={{ display: "flex", gap: "10px", margin: '30px 0' }}>
                <motion.div
                  style={{ backgroundColor: "#48662a", color: "white", padding: "13px 15px 10px 13px", borderRadius: "50%" }}
                  className='lovedsffffficn'
                  whileHover={{ scale: 1.15 }}
                >
                  <FaUser />
                </motion.div>
                <div style={{ display: "flex", flexDirection: "column" }}>
                  <span>{name}</span>
                  <span>{biz}</span>
                </div>
              </div>
            </motion.div>
          ))}
        </motion.div>
      </section>

      {/* ── CTA Section ── */}
      <motion.section
        style={{ marginBottom: '15%' }}
        className='beforethefootdec'
        variants={staggerContainer}
        initial="hidden"
        whileInView="visible"
        viewport={vp}
      >
        <div>
          <motion.p variants={fadeLeft}>
            Take control of your <br />business finances.
          </motion.p>
          <motion.p
            style={{ textAlign: "start", fontSize: "20px", width: "60%" }}
            className='beforethefootdecnote'
            variants={fadeRight}
          >
            Get ahead of financial problems before they happen. Loamy works quietly in the background so you can focus on growth.
          </motion.p>
          <motion.div
            style={{ display: "flex", gap: "20px", margin: "10px 0" }}
            className='bffoobtnsec'
            variants={staggerContainerFast}
          >
            <motion.button
              style={{ padding: "15px 30px", backgroundColor: 'white' }}
              variants={fadeLeft}
              whileHover={{ scale: 1.05 }}
              whileTap={{ scale: 0.96 }}
            >
              Start Free Today <FaArrowRight style={{ position: "relative", top: "2px" }} />
            </motion.button>
            <motion.button
              style={{ backgroundColor: "transparent", border: '1px gray solid', color: "white" }}
              variants={fadeRight}
              whileHover={{ scale: 1.05 }}
              whileTap={{ scale: 0.96 }}
            >
              Talk to us
            </motion.button>
          </motion.div>
        </div>
      </motion.section>

      {/* ── Footer ── */}
      <motion.footer
        className='lastfooter'
        variants={staggerContainer}
        initial="hidden"
        whileInView="visible"
        viewport={{ once: true, amount: 0.1 }}
      >
        <motion.div style={{ position: "relative", left: "-10px" }} variants={fadeLeft}>
          <div style={{ margin: "10px 0" }}>Loamy</div>
          <p style={{ margin: "30px 0" }}>AI-powered financial awareness for <br />African small medium businesses</p>
          <motion.div
            style={{ position: "relative", left: "-10px", top: "10px" }}
            variants={staggerContainerFast}
          >
            {['Twitter', 'LinkedIn', 'Instagram'].map((s) => (
              <motion.span
                key={s}
                style={{ padding: "10px 20px", backgroundColor: 'white', margin: "5px", borderRadius: "10px", fontSize: "12px" }}
                variants={fadeUp}
                whileHover={{ scale: 1.08, y: -3 }}
              >
                {s}
              </motion.span>
            ))}
          </motion.div>
        </motion.div>

        <motion.div
          style={{ display: "flex", gap: "100px", position: "relative", left: "40px" }}
          className='minghlastfooter'
          variants={staggerContainer}
        >
          {[
            { title: 'PRODUCT', items: ['Featured', 'How it works', 'Pricing', 'Changelog'] },
            { title: 'COMPANY', items: ['About', 'Customers', 'Careers', 'Contact'] },
            { title: 'RESOURCES', items: ['Blog', 'Help center', 'Security', 'Status'] },
            { title: 'LEGAL', items: ['Terms', 'Privacy', 'Cookies', 'DPA'] },
          ].map(({ title, items }, i) => {
            const colDirs = [fadeLeft, fadeDown, fadeUp, fadeRight];
            return (
              <motion.div key={i} variants={colDirs[i]}>
                <p>{title}</p>
                <ul style={{ listStyle: "none", position: "relative", left: "-40px" }}>
                  {items.map((item) => (
                    <motion.li key={item} whileHover={{ x: 6, color: '#48662a' }} transition={{ type: 'spring', stiffness: 300 }}>
                      {item}
                    </motion.li>
                  ))}
                </ul>
              </motion.div>
            );
          })}
        </motion.div>
      </motion.footer>

      <hr />

      <motion.div
        style={{ display: "flex", justifyContent: "space-around" }}
        className='lastlastlastfooter'
        initial={{ opacity: 0, y: 30 }}
        whileInView={{ opacity: 1, y: 0 }}
        viewport={{ once: true }}
        transition={{ duration: 0.5 }}
      >
        <p>2026 Loamy. Made with care for African businesses.</p>
        <motion.div
          className='countrirs'
          variants={staggerContainerFast}
          initial="hidden"
          whileInView="visible"
          viewport={{ once: true }}
        >
          <ol style={{ display: "flex" }}>
            {['Lagos', 'Accra', 'Nairobi', 'Cape Town'].map((city) => (
              <motion.ul key={city} variants={fadeUp} whileHover={{ y: -4, color: '#48662a' }}>
                {city}
              </motion.ul>
            ))}
          </ol>
        </motion.div>
      </motion.div>

    </div>
  );
};

export default LoamyHomePage;
